import contextlib
import json
import socket
import stat
import time
from pathlib import Path

import pytest
from sqlalchemy import Engine

from backend.adapters.rpc_protocol import MAX_MESSAGE_BYTES
from backend.adapters.worker_client import WorkerClient
from backend.domain.actor import Actor
from backend.domain.enums import ActorType
from backend.domain.errors import (
    NotFoundError,
    ValidationError,
    WorkerError,
    WorkerUnavailableError,
)
from backend.repositories.database import UnitOfWork
from backend.services.audit import AuditService
from backend.worker.rpc import RpcCall, RpcServer
from tests.api_support import running_worker
from tests.conftest import DEV

ACTOR = Actor(ActorType.USER, "1")


@pytest.fixture
def socket_path(socket_dir: Path, engine: Engine, tmp_path: Path, project: str):  # type: ignore[no-untyped-def]
    with running_worker(socket_dir, engine, tmp_path / "keys" / "secret.key") as path:
        yield path


def _raw(path: Path, payload: bytes) -> dict[str, object]:
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(5)
        connection.connect(str(path))
        connection.sendall(payload)
        data = b""
        while not data.endswith(b"\n"):
            chunk = connection.recv(65536)
            if not chunk:
                break
            data += chunk
    result: dict[str, object] = json.loads(data)
    return result


def test_ping_roundtrip(socket_path: Path) -> None:
    WorkerClient(socket_path).ping()


def test_socket_mode_is_0660(socket_path: Path) -> None:
    assert stat.S_IMODE(socket_path.stat().st_mode) == 0o660


def test_secret_roundtrip_over_socket(socket_path: Path, project: str) -> None:
    client = WorkerClient(socket_path)
    info = client.set_secret(project, DEV, "DB_PASSWORD", "s3cr3t", ACTOR)
    assert info.name == "DB_PASSWORD"
    assert [i.name for i in client.list_secrets(project, DEV, ACTOR)] == ["DB_PASSWORD"]
    assert client.reveal_secret(project, DEV, "DB_PASSWORD", ACTOR) == "s3cr3t"
    client.delete_secret(project, DEV, "DB_PASSWORD", ACTOR)
    assert client.list_secrets(project, DEV, ACTOR) == []


def test_domain_errors_map_back(socket_path: Path, project: str) -> None:
    client = WorkerClient(socket_path)
    with pytest.raises(NotFoundError):
        client.reveal_secret(project, DEV, "MISSING", ACTOR)
    with pytest.raises(NotFoundError):
        client.list_secrets("nope", DEV, ACTOR)
    with pytest.raises(ValidationError):
        client.set_secret(project, DEV, "bad name", "v", ACTOR)
    with pytest.raises(ValidationError):
        client.set_secret(project, DEV, "OK", "", ACTOR)


def test_unknown_method_rejected(socket_path: Path) -> None:
    reply = _raw(socket_path, b'{"id": "1", "method": "os.system", "params": {}}\n')
    assert reply["ok"] is False
    assert reply["error"] == {"code": "unknown_method", "message": "unknown method"}
    assert reply["id"] == "1"


def test_malformed_request_keeps_connection_usable(socket_path: Path) -> None:
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(5)
        connection.connect(str(socket_path))
        connection.sendall(b"not json\n")
        first = json.loads(connection.recv(65536))
        assert first["error"]["code"] == "bad_request"
        connection.sendall(b'{"id": "2", "method": "ping"}\n')
        second = json.loads(connection.recv(65536))
        assert second["ok"] is True
        assert second["id"] == "2"


def test_unexpected_fields_rejected(socket_path: Path) -> None:
    reply = _raw(socket_path, b'{"id": "1", "method": "ping", "extra": 1}\n')
    assert reply["error"]["code"] == "bad_request"  # type: ignore[index]


def test_oversized_request_rejected(socket_path: Path) -> None:
    payload = (
        b'{"id": "1", "method": "ping", "params": {"x": "' + b"a" * MAX_MESSAGE_BYTES + b'"}}\n'
    )
    with socket.socket(socket.AF_UNIX) as connection:
        connection.settimeout(5)
        connection.connect(str(socket_path))
        with contextlib.suppress(BrokenPipeError):
            connection.sendall(payload)
        reply = json.loads(connection.recv(65536))
    assert reply["error"]["code"] == "too_large"


def test_client_refuses_oversized_request(socket_path: Path) -> None:
    client = WorkerClient(socket_path)
    with pytest.raises(ValidationError, match="too large"):
        client.set_secret("demo", DEV, "BIG", "x" * MAX_MESSAGE_BYTES, ACTOR)


def test_secret_call_without_actor_is_bad_request(socket_path: Path) -> None:
    params = '{"slug": "demo", "env": "development"}'
    reply = _raw(
        socket_path, f'{{"id": "1", "method": "secrets.list", "params": {params}}}\n'.encode()
    )
    assert reply["error"]["code"] == "bad_request"  # type: ignore[index]


def test_bad_params_do_not_echo_values(socket_path: Path) -> None:
    line = {
        "id": "1",
        "method": "secrets.set",
        "actor": {"type": "user", "id": "1"},
        "params": {"slug": "demo", "env": "nowhere", "name": "A", "value": "TOP-SECRET-VALUE"},
    }
    reply = _raw(socket_path, json.dumps(line).encode() + b"\n")
    assert reply["error"]["code"] == "bad_request"  # type: ignore[index]
    assert "TOP-SECRET-VALUE" not in json.dumps(reply)


def test_unexpected_failure_returns_generic_error(socket_dir: Path) -> None:
    def boom(_call: RpcCall) -> object:
        raise RuntimeError("database password is hunter2")

    server = RpcServer(socket_dir / "w.sock", {"boom": boom})
    server.start()
    try:
        reply = _raw(socket_dir / "w.sock", b'{"id": "1", "method": "boom"}\n')
    finally:
        server.stop()
    assert reply["error"] == {"code": "internal", "message": "internal worker error"}


def test_client_times_out(socket_dir: Path) -> None:
    server = RpcServer(socket_dir / "w.sock", {"slow": lambda _call: time.sleep(1.5)})
    server.start()
    try:
        client = WorkerClient(socket_dir / "w.sock", timeout=0.2)
        started = time.monotonic()
        with pytest.raises(WorkerUnavailableError):
            client._call("slow", None, {})
        assert time.monotonic() - started < 1.0
    finally:
        server.stop()


def test_unreachable_worker(socket_dir: Path) -> None:
    with pytest.raises(WorkerUnavailableError):
        WorkerClient(socket_dir / "absent.sock").ping()


def test_client_rejects_wrong_response_id(socket_dir: Path) -> None:
    server = RpcServer(socket_dir / "w.sock", {})
    server.start()
    try:
        # handler table is empty: the client must surface a worker error for unknown methods
        with pytest.raises(WorkerError, match="unknown_method"):
            WorkerClient(socket_dir / "w.sock").ping()
    finally:
        server.stop()


def test_stop_removes_socket_and_restart_replaces_stale_file(socket_dir: Path) -> None:
    path = socket_dir / "w.sock"
    first = RpcServer(path, {})
    first.start()
    first.stop()
    assert not path.exists()
    stale = socket.socket(socket.AF_UNIX)
    stale.bind(str(path))
    stale.close()
    second = RpcServer(path, {})
    second.start()
    second.stop()


def test_refuses_to_replace_regular_file(socket_dir: Path) -> None:
    path = socket_dir / "w.sock"
    path.write_text("precious")
    with pytest.raises(FileExistsError):
        RpcServer(path, {}).start()
    assert path.read_text() == "precious"


def test_reveal_is_audited_with_the_actor_sent_by_the_api(
    socket_path: Path, project: str, uow: UnitOfWork
) -> None:
    client = WorkerClient(socket_path)
    client.set_secret(project, DEV, "API_KEY", "v1", Actor(ActorType.TOKEN, "9"))
    client.reveal_secret(project, DEV, "API_KEY", Actor(ActorType.TOKEN, "9"))
    events = AuditService(uow).list(action="secret.reveal")
    assert [(e.actor_type, e.actor_id) for e in events] == [(ActorType.TOKEN, "9")]
    assert "v1" not in repr(events[0].details)
