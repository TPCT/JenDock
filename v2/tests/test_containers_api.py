"""Container and terminal endpoints through a real worker RPC server on fake Docker."""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from shiftcodes.adapters.container_admin import FileRead
from shiftcodes.adapters.docker_stats import ContainerStats
from shiftcodes.api.container import AppContainer
from shiftcodes.api.container_logs import ContainerLogStream
from shiftcodes.api.principal import Principal
from shiftcodes.containers.base import ContainerAuditor
from shiftcodes.containers.logs import LogCursor
from shiftcodes.containers.wire import LogCursorView, LogLineView, LogPageView
from shiftcodes.domain.enums import ActorType, EnvironmentName
from shiftcodes.domain.errors import NotFoundError
from shiftcodes.repositories.database import UnitOfWork, create_session_factory
from shiftcodes.services.audit import AuditService
from shiftcodes.services.secret_cipher import SecretCipher
from shiftcodes.services.secrets import SecretService
from shiftcodes.services.sessions import SessionContext
from shiftcodes.terminal.opener import AuditTerminalListener
from shiftcodes.terminal.server import TerminalServer
from shiftcodes.terminal.session import TerminalLimits, TerminalManager
from shiftcodes.worker.container_handlers import build_container_handlers
from shiftcodes.worker.handlers import build_handlers
from shiftcodes.worker.rpc import RpcServer
from tests.api_support import (
    FakeClock,
    create_user,
    login,
    make_client,
    make_container,
    make_settings,
    mutation_headers,
)
from tests.container_harness import Stack, build_stack
from tests.terminal_support import ShellOpener, wait_for

ORIGIN = "https://app.example.test"
BASE = "/api/projects/demo/environments/development/containers"


@dataclass
class Wired:
    """The API wired to a worker with fake Docker and a shell-backed terminal server."""

    container: AppContainer
    client: TestClient
    csrf: str
    stack: Stack
    engine: Engine

    @property
    def web(self) -> str:
        """Id of the web container."""
        return self.stack.web_id

    def headers(self) -> dict[str, str]:
        """Origin and CSRF of the logged-in session."""
        return mutation_headers(self.client, self.csrf)

    def token(self, scopes: list[str]) -> dict[str, str]:
        """A bearer header for a new token with ``scopes``."""
        body = {"name": "t", "scopes": scopes}
        response = self.client.post("/api/tokens", json=body, headers=self.headers())
        return {"Authorization": f"Bearer {response.json()['token']}"}

    def ws_headers(self) -> dict[str, str]:
        """Origin and session cookie of the logged-in browser (httpx would not send a Secure
        cookie to a ``ws://`` test URL, a real browser sends it over ``wss://``)."""
        name = self.container.cookies.name
        return {"Origin": ORIGIN, "Cookie": f"{name}={self.client.cookies.get(name)}"}

    def bare(self) -> TestClient:
        """A client without cookies."""
        return TestClient(self.client.app, base_url=str(self.client.base_url))

    def audit(self, action: str) -> list[dict[str, object]]:
        """Audit details of ``action``."""
        service = AuditService(self.container.uow())
        return [dict(event.details) for event in service.list(action=action)]


@pytest.fixture
def wired(
    tmp_path: Path, socket_dir: Path, engine: Engine, clock: FakeClock, project: str
) -> Iterator[Wired]:
    stack = build_stack(tmp_path, engine, project)
    factory = create_session_factory(engine)
    cipher = SecretCipher(tmp_path / "worker-only" / "secret.key")
    handlers = build_handlers(
        lambda: SecretService(UnitOfWork(factory), cipher), build_container_handlers(stack.services)
    )
    settings = make_settings(tmp_path, socket_path=socket_dir / "w.sock")
    rpc = RpcServer(settings.worker_socket_path, handlers)
    manager = TerminalManager(
        TerminalLimits(idle_seconds=60, max_seconds=60),
        AuditTerminalListener(ContainerAuditor(factory)),
    )
    terminal = TerminalServer(settings.worker_terminal_socket_path, manager, ShellOpener())
    rpc.start()
    manager.start()
    terminal.start()
    try:
        container = make_container(settings, engine, clock)
        create_user(container)
        client = make_client(container)
        yield Wired(container, client, login(client), stack, engine)
    finally:
        terminal.stop()
        manager.shutdown()
        rpc.stop()


# --- inventory --------------------------------------------------------------------


def test_inventory_and_detail_shapes(wired: Wired) -> None:
    listing = wired.client.get(BASE)
    assert listing.status_code == 200
    items = listing.json()
    assert [item["service"] for item in items] == ["web", "worker"]
    web = items[0]
    assert set(web) == {
        "id", "name", "service", "image", "image_id", "revision", "commit", "state", "health",
        "restart_count", "started_at", "exit_code", "cpus", "memory_bytes", "ports", "mounts",
    }  # fmt: skip
    assert web["ports"] == [
        {"container_port": "8080/tcp", "host_ip": "127.0.0.1", "host_port": "8080"}
    ]
    assert "hunter2-secret" not in listing.text
    assert wired.client.get(f"{BASE}/{wired.web}").json()["id"] == wired.web


def test_foreign_unknown_and_malformed_ids(wired: Wired) -> None:
    assert wired.client.get(f"{BASE}/{wired.stack.foreign_id}").status_code == 404
    assert wired.client.get(f"{BASE}/{'0' * 64}").status_code == 404
    short = wired.client.get(f"{BASE}/{wired.web[:12]}")
    assert short.status_code == 422 and short.json()["error"]["code"] == "validation_error"


def test_authentication_and_scopes(wired: Wired) -> None:
    anonymous = wired.bare()
    assert anonymous.get(BASE).status_code == 401
    reader = wired.token(["read"])
    containers = wired.token(["containers"])
    assert anonymous.get(BASE, headers=reader).status_code == 200
    assert anonymous.get(f"{BASE}/{wired.web}/env", headers=reader).status_code == 200
    assert anonymous.get(f"{BASE}/{wired.web}/logs", headers=reader).status_code == 403
    assert anonymous.get(f"{BASE}/{wired.web}/files", headers=reader).status_code == 403
    plan = {"action": "restart"}
    assert (
        anonymous.post(f"{BASE}/{wired.web}/actions", json=plan, headers=reader).status_code == 403
    )
    assert (
        anonymous.post(f"{BASE}/{wired.web}/actions", json=plan, headers=containers).status_code
        == 200
    )
    assert anonymous.get("/api/telemetry/host", headers=reader).status_code == 200


# --- lifecycle --------------------------------------------------------------------


def test_two_step_restart_requires_csrf_and_a_valid_confirmation(wired: Wired) -> None:
    url = f"{BASE}/{wired.web}/actions"
    origin_only = {"Origin": ORIGIN}
    assert (
        wired.client.post(url, json={"action": "restart"}, headers=origin_only).status_code == 403
    )
    plan = wired.client.post(url, json={"action": "restart"}, headers=wired.headers())
    assert plan.status_code == 200
    body = plan.json()
    assert body["action"] == "restart" and body["container"]["id"] == wired.web
    assert body["effects"] and body["confirmation"]
    assert not any(call[0] == "restart" for call in wired.stack.engine.calls)
    confirm = f"{url}/confirm"
    bad = wired.client.post(confirm, json={"confirmation": "wrong"}, headers=wired.headers())
    assert bad.status_code == 422
    done = wired.client.post(
        confirm, json={"confirmation": body["confirmation"]}, headers=wired.headers()
    )
    assert done.status_code == 200 and done.json()["container"]["restart_count"] == 1
    again = wired.client.post(
        confirm, json={"confirmation": body["confirmation"]}, headers=wired.headers()
    )
    assert again.status_code == 422
    audit = wired.audit("container.restart")
    assert audit[0]["outcome"] == "done"


def test_confirmation_cannot_be_used_by_another_principal(wired: Wired) -> None:
    url = f"{BASE}/{wired.web}/actions"
    plan = wired.client.post(url, json={"action": "stop"}, headers=wired.headers()).json()
    token = wired.token(["containers"])
    stolen = wired.bare().post(
        f"{url}/confirm", json={"confirmation": plan["confirmation"]}, headers=token
    )
    assert stolen.status_code == 422
    assert not any(call[0] == "stop" for call in wired.stack.engine.calls)


def test_invalid_action_is_rejected(wired: Wired) -> None:
    response = wired.client.post(
        f"{BASE}/{wired.web}/actions", json={"action": "rm"}, headers=wired.headers()
    )
    assert response.status_code == 422


# --- environment ------------------------------------------------------------------


def test_env_listing_reveal_and_update(wired: Wired) -> None:
    listing = wired.client.get(f"{BASE}/{wired.web}/env")
    names = {item["name"]: item for item in listing.json()}
    assert names["DB_PASSWORD"]["sensitive"] is True
    assert names["DB_PASSWORD"]["masked_value"] == "********"
    assert "hunter2-secret" not in listing.text
    containers = wired.token(["containers"])
    forbidden = wired.bare().post(f"{BASE}/{wired.web}/env/DB_PASSWORD/reveal", headers=containers)
    assert forbidden.status_code == 403
    reveal = wired.client.post(
        f"{BASE}/{wired.web}/env/DB_PASSWORD/reveal", headers=wired.headers()
    )
    assert reveal.json() == {"name": "DB_PASSWORD", "value": "hunter2-secret"}
    assert wired.audit("container.env.reveal")[0]["name"] == "DB_PASSWORD"
    update = wired.client.put(
        f"{BASE}/{wired.web}/env",
        json={"set": {"MODE": "turbo"}, "remove": []},
        headers=wired.headers(),
    )
    assert update.status_code == 200
    body = update.json()
    assert body["restarted"] is True and body["containers"][0]["state"] == "running"
    assert "turbo" not in update.text
    new_id = body["containers"][0]["id"]
    assert new_id != wired.web
    flagged = {i["name"]: i["overridden"] for i in wired.client.get(f"{BASE}/{new_id}/env").json()}
    assert flagged["MODE"] is True and flagged["PATH"] is False


def test_env_update_validation_and_failure_are_reported(wired: Wired) -> None:
    url = f"{BASE}/{wired.web}/env"
    assert (
        wired.client.put(url, json={"set": {"1x": "v"}}, headers=wired.headers()).status_code == 422
    )
    assert wired.client.put(url, json={"nope": 1}, headers=wired.headers()).status_code == 422
    wired.stack.compose.crash_when = ("MODE", "bad")
    failed = wired.client.put(url, json={"set": {"MODE": "bad"}}, headers=wired.headers())
    assert failed.status_code == 409
    assert "restored" in failed.json()["error"]["message"]
    assert "bad" not in failed.json()["error"]["message"].replace("the previous", "")


# --- limits, files, logs, stats ---------------------------------------------------


def test_limits(wired: Wired) -> None:
    url = f"{BASE}/{wired.web}/limits"
    ok = wired.client.put(url, json={"cpus": 1.5, "memory": "256m"}, headers=wired.headers())
    assert ok.status_code == 200
    assert ok.json()["cpus"] == 1.5 and ok.json()["memory_bytes"] == 256 * 1024**2
    too_much = wired.client.put(url, json={"memory": "999g"}, headers=wired.headers())
    assert too_much.status_code == 422
    assert wired.client.put(url, json={}, headers=wired.headers()).status_code == 422
    assert wired.client.put(url, json={"cpus": -1}, headers=wired.headers()).status_code == 422


def test_file_browser_endpoints(wired: Wired) -> None:
    wired.stack.engine.directories["/app"] = ["src/", "run.sh"]
    wired.stack.engine.files["/app/run.sh"] = FileRead("file", 9, b"echo hi\n\n", False)
    listing = wired.client.get(f"{BASE}/{wired.web}/files", params={"path": "/app"})
    assert listing.json() == {
        "path": "/app",
        "entries": [{"name": "src", "kind": "directory"}, {"name": "run.sh", "kind": "file"}],
        "truncated": False,
    }
    content = wired.client.get(f"{BASE}/{wired.web}/files/content", params={"path": "/app/run.sh"})
    assert content.json()["text"].startswith("echo hi")
    for bad in ("/app/../etc/passwd", "etc/passwd", "/proc/1/environ", "/app/.env"):
        assert (
            wired.client.get(f"{BASE}/{wired.web}/files", params={"path": bad}).status_code == 422
        )
        response = wired.client.get(f"{BASE}/{wired.web}/files/content", params={"path": bad})
        assert response.status_code == 422
    assert (
        wired.client.get(f"{BASE}/{wired.web}/files", params={"path": "/nope"}).status_code == 404
    )


def test_log_tail_endpoint(wired: Wired) -> None:
    wired.stack.engine.log_lines[wired.web] = [
        "2026-10-13T12:00:01.000000000Z started with hunter2-secret",
        "2026-10-13T12:00:02.000000000Z ready",
    ]
    response = wired.client.get(f"{BASE}/{wired.web}/logs", params={"tail": 5})
    body = response.json()
    assert [line["text"] for line in body["lines"]] == ["started with [REDACTED]", "ready"]
    assert body["cursor"]["since"] == "2026-10-13T12:00:02.000000000Z"
    assert wired.client.get(f"{BASE}/{wired.web}/logs", params={"tail": 100000}).status_code == 422
    follow = wired.client.get(
        f"{BASE}/{wired.web}/logs",
        params={"since": body["cursor"]["since"], "skip": body["cursor"]["skip"]},
    )
    assert follow.json()["lines"] == []


def test_stats_and_host_telemetry(wired: Wired) -> None:
    wired.stack.engine.sample_stats = [
        ContainerStats(wired.web, "web", 3.5, 1000, 2000, 50.0, 1, 2, 3, 4, 5)
    ]
    stats = wired.client.get(f"{BASE}/{wired.web}/stats").json()
    assert stats["latest"]["cpu_percent"] == 3.5 and len(stats["history"]) == 1
    assert wired.client.get(f"{BASE}/{wired.stack.other_id}/stats").status_code == 409
    host = wired.client.get("/api/telemetry/host").json()
    assert host["latest"]["memory_total"] > 0 and host["latest"]["disk_total"] > 0
    assert host["history"]


def test_worker_down_is_a_503(
    tmp_path: Path, engine: Engine, clock: FakeClock, project: str
) -> None:
    container = make_container(make_settings(tmp_path), engine, clock)
    create_user(container)
    client = make_client(container)
    login(client)
    response = client.get(BASE)
    assert response.status_code == 503 and response.json()["error"]["code"] == "worker_unavailable"


# --- SSE log stream ---------------------------------------------------------------


class _ScriptedGateway:
    """Returns scripted log pages, then raises ``NotFoundError`` (ends the stream)."""

    def __init__(self, pages: Sequence[LogPageView]) -> None:
        self._pages = list(pages)
        self.cursors: list[LogCursor | None] = []

    def logs(
        self, *_args: object, tail: int, cursor: LogCursor | None, **_kw: object
    ) -> LogPageView:
        self.cursors.append(cursor)
        if not self._pages:
            raise NotFoundError("container not found")
        return self._pages.pop(0)


def test_log_stream_events_are_bounded_masked_json(wired: Wired) -> None:
    since = "2026-10-13T12:00:01.000000000Z"
    page = LogPageView(
        lines=(LogLineView(timestamp=since, text="hello\nworld"),),
        cursor=LogCursorView(since=since, skip=1),
        dropped=3,
    )
    gateway = _ScriptedGateway([page])
    container = dataclasses.replace(wired.container, containers=gateway)  # type: ignore[arg-type]
    context = SessionContext(1, 1, "admin", "csrf", datetime(2030, 1, 1, tzinfo=UTC))
    stream = ContainerLogStream(
        container, Principal(session=context), "demo", EnvironmentName.DEVELOPMENT, "a" * 64, 10
    )
    events = list(stream)
    first = events[0]
    assert first.startswith("event: log\ndata: ") and first.endswith("\n\n")
    data = first.split("data: ", 1)[1].strip()
    assert "\n" not in data  # one SSE data line
    payload = json.loads(data)
    assert payload["lines"][0]["text"] == "hello\nworld" and payload["dropped"] == 3
    assert events[-1].startswith("event: error")
    assert gateway.cursors == [None, LogCursor(since, 1)]


# --- terminal ---------------------------------------------------------------------


def _ticket(wired: Wired, container_id: str | None = None, **body: object) -> str:
    response = wired.client.post(
        f"{BASE}/{container_id or wired.web}/terminal/tickets", json=body, headers=wired.headers()
    )
    assert response.status_code == 200, response.text
    assert response.json()["websocket_path"] == "/api/terminal/ws"
    return str(response.json()["ticket"])


def _read_until(socket: WebSocketTestSession, needle: bytes, limit: int = 200) -> bytes:
    output: bytes = b""
    for _ in range(limit):
        message = socket.receive()
        if message.get("bytes") is not None:
            output += bytes(message["bytes"])
            if needle in output:
                return output
        elif message.get("text") is not None:
            event = json.loads(message["text"])
            if event["type"] in {"exit", "error"}:
                return output
    return output


def _auth(socket: WebSocketTestSession, ticket: str, **extra: object) -> dict[str, object]:
    socket.send_text(json.dumps({"type": "auth", "ticket": ticket, **extra}))
    event = json.loads(socket.receive_text())
    assert isinstance(event, dict)
    return event


def test_terminal_session_runs_a_command_and_exits_and_is_audited(wired: Wired) -> None:
    ticket = _ticket(wired)
    with wired.client.websocket_connect("/api/terminal/ws", headers=wired.ws_headers()) as socket:
        ready = _auth(socket, ticket, rows=24, cols=80)
        assert ready["type"] == "ready" and ready["terminal_id"]
        socket.send_bytes(b"echo hi\n")
        output = _read_until(socket, b"\r\nhi\r\n")
        assert b"\r\nhi\r\n" in output
        socket.send_text(json.dumps({"type": "resize", "rows": 31, "cols": 101}))
        socket.send_bytes(b"stty size\n")
        assert b"31 101" in _read_until(socket, b"31 101")
        socket.send_text(json.dumps({"type": "ping"}))
        socket.send_bytes(b"exit\n")
        tail = _read_until(socket, b"never")
        del tail
    assert wait_for(lambda: len(wired.audit("terminal.close")) == 1)
    opened, closed = wired.audit("terminal.open")[0], wired.audit("terminal.close")[0]
    assert opened["service"] == "web" and closed["reason"] == "exited"
    assert closed["exit_code"] == 0 and isinstance(closed["duration_seconds"], float)
    everything = json.dumps([wired.audit("terminal.open"), wired.audit("terminal.close")])
    assert "echo hi" not in everything and "stty" not in everything


def test_ticket_is_single_use(wired: Wired) -> None:
    ticket = _ticket(wired)
    with wired.client.websocket_connect("/api/terminal/ws", headers=wired.ws_headers()) as socket:
        assert _auth(socket, ticket)["type"] == "ready"
        socket.send_text(json.dumps({"type": "close"}))
        _read_until(socket, b"never")
    with wired.client.websocket_connect("/api/terminal/ws", headers=wired.ws_headers()) as socket:
        socket.send_text(json.dumps({"type": "auth", "ticket": ticket}))
        with pytest.raises(WebSocketDisconnect) as closed:
            socket.receive_text()
        assert closed.value.code == 1008


def test_websocket_origin_and_credentials_are_enforced(wired: Wired) -> None:
    ticket = _ticket(wired)
    with (
        pytest.raises(WebSocketDisconnect),
        wired.client.websocket_connect("/api/terminal/ws", headers={"Origin": "https://evil.test"}),
    ):
        pass
    # a session ticket without the session cookie (another browser) is refused
    stranger = wired.bare()
    with stranger.websocket_connect("/api/terminal/ws", headers={"Origin": ORIGIN}) as socket:
        socket.send_text(json.dumps({"type": "auth", "ticket": ticket}))
        with pytest.raises(WebSocketDisconnect):
            socket.receive_text()
    # the ticket was consumed by the failed attempt
    with wired.client.websocket_connect("/api/terminal/ws", headers=wired.ws_headers()) as socket:
        socket.send_text(json.dumps({"type": "auth", "ticket": ticket}))
        with pytest.raises(WebSocketDisconnect):
            socket.receive_text()
    # a session ticket without an Origin header is refused as well
    ticket = _ticket(wired)
    cookie_only = {"Cookie": wired.ws_headers()["Cookie"]}
    with wired.client.websocket_connect("/api/terminal/ws", headers=cookie_only) as socket:
        socket.send_text(json.dumps({"type": "auth", "ticket": ticket}))
        with pytest.raises(WebSocketDisconnect):
            socket.receive_text()


def test_websocket_rejects_garbage_handshakes(wired: Wired) -> None:
    for first in (
        "not json",
        json.dumps({"type": "hello"}),
        json.dumps({"type": "auth"}),
        "x" * 5000,
    ):
        with wired.client.websocket_connect(
            "/api/terminal/ws", headers=wired.ws_headers()
        ) as socket:
            socket.send_text(first)
            with pytest.raises(WebSocketDisconnect):
                socket.receive_text()


def test_ticket_endpoint_checks(wired: Wired) -> None:
    url = f"{BASE}/{wired.web}/terminal/tickets"
    assert wired.client.post(url, json={}, headers={"Origin": ORIGIN}).status_code == 403  # no CSRF
    assert wired.bare().post(url, json={}).status_code == 401
    read_only = wired.token(["read", "containers"])
    assert wired.bare().post(url, json={}, headers=read_only).status_code == 403
    stopped = f"{BASE}/{wired.stack.other_id}/terminal/tickets"
    assert wired.client.post(stopped, json={}, headers=wired.headers()).status_code == 409
    foreign = f"{BASE}/{wired.stack.foreign_id}/terminal/tickets"
    assert wired.client.post(foreign, json={}, headers=wired.headers()).status_code == 404
    assert wired.client.post(url, json={"x": 1}, headers=wired.headers()).status_code == 422


def test_token_with_terminal_scope_can_use_the_ticket_flow(wired: Wired) -> None:
    headers = wired.token(["terminal"])
    anonymous = wired.bare()
    response = anonymous.post(f"{BASE}/{wired.web}/terminal/tickets", json={}, headers=headers)
    assert response.status_code == 200
    ticket = response.json()["ticket"]
    with anonymous.websocket_connect("/api/terminal/ws") as socket:
        assert _auth(socket, ticket)["type"] == "ready"
        socket.send_bytes(b"echo from-token\n")
        assert b"from-token" in _read_until(socket, b"\r\nfrom-token\r\n")
        socket.send_text(json.dumps({"type": "close"}))
        _read_until(socket, b"never")
    assert wait_for(lambda: len(wired.audit("terminal.close")) == 1)
    events = AuditService(wired.container.uow()).list(action="terminal.open")
    assert events[0].actor_type == ActorType.TOKEN


def test_reattach_with_a_new_ticket(wired: Wired) -> None:
    with wired.client.websocket_connect("/api/terminal/ws", headers=wired.ws_headers()) as socket:
        ready = _auth(socket, _ticket(wired))
        terminal_id = str(ready["terminal_id"])
        socket.send_bytes(b"KEEP=alive\n")
        time.sleep(0.2)
    ticket = _ticket(wired, terminal_id=terminal_id)
    with wired.client.websocket_connect("/api/terminal/ws", headers=wired.ws_headers()) as socket:
        ready = _auth(socket, ticket)
        assert ready["terminal_id"] == terminal_id
        socket.send_bytes(b"echo $KEEP\n")
        assert b"\r\nalive\r\n" in _read_until(socket, b"\r\nalive\r\n")
        socket.send_text(json.dumps({"type": "close"}))
        _read_until(socket, b"never")


def test_closed_terminal_is_gone(wired: Wired) -> None:
    ticket = _ticket(wired)
    with wired.client.websocket_connect("/api/terminal/ws", headers=wired.ws_headers()) as socket:
        terminal_id = str(_auth(socket, ticket)["terminal_id"])
        socket.send_text(json.dumps({"type": "close"}))
        _read_until(socket, b"never")
    assert wait_for(lambda: len(wired.audit("terminal.close")) == 1)
    again = _ticket(wired, terminal_id=terminal_id)
    with wired.client.websocket_connect("/api/terminal/ws", headers=wired.ws_headers()) as socket:
        event = _auth(socket, again)
        assert event["type"] == "error" and "not found" in str(event["message"])


def _terminal_token(wired: Wired) -> tuple[int, dict[str, str]]:
    body = {"name": "t", "scopes": ["terminal"]}
    created = wired.client.post("/api/tokens", json=body, headers=wired.headers()).json()
    return int(created["info"]["id"]), {"Authorization": f"Bearer {created['token']}"}


def test_token_terminal_ends_and_is_audited_when_the_token_is_revoked(
    wired: Wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shiftcodes.api.terminal_bridge.SESSION_CHECK_SECONDS", 0.2)
    token_id, headers = _terminal_token(wired)
    anonymous = wired.bare()
    url = f"{BASE}/{wired.web}/terminal/tickets"
    ticket = anonymous.post(url, json={}, headers=headers).json()["ticket"]
    with anonymous.websocket_connect("/api/terminal/ws") as socket:
        assert _auth(socket, ticket)["type"] == "ready"
        revoked = wired.client.delete(f"/api/tokens/{token_id}", headers=wired.headers())
        assert revoked.status_code == 200
        ended = None
        for _ in range(100):
            message = socket.receive()
            if message["type"] == "websocket.close":
                ended = message["code"]
                break
        assert ended == 4401
    assert wait_for(lambda: len(wired.audit("terminal.close")) == 1)
    assert wired.audit("terminal.close")[0]["reason"] == "access_revoked"


def test_session_terminal_ends_and_is_audited_on_logout(
    wired: Wired, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shiftcodes.api.terminal_bridge.SESSION_CHECK_SECONDS", 0.2)
    ticket = _ticket(wired)
    with wired.client.websocket_connect("/api/terminal/ws", headers=wired.ws_headers()) as socket:
        assert _auth(socket, ticket)["type"] == "ready"
        assert wired.client.post("/api/auth/logout", headers=wired.headers()).status_code == 204
        for _ in range(100):
            if socket.receive()["type"] == "websocket.close":
                break
    assert wait_for(lambda: len(wired.audit("terminal.close")) == 1)
    assert wired.audit("terminal.close")[0]["reason"] == "access_revoked"
