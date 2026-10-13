"""The worker's GitHub RPC methods, reached through the real client and socket."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from shiftcodes.adapters.errors import SourceError
from shiftcodes.adapters.worker_client import WorkerClient
from shiftcodes.domain.actor import Actor
from shiftcodes.domain.enums import ActorType, GitTransport
from shiftcodes.domain.errors import (
    ConfigurationError,
    NotFoundError,
    SourceUnavailableError,
    ValidationError,
)
from shiftcodes.domain.git import BranchHead
from shiftcodes.repositories.database import UnitOfWork, create_session_factory
from shiftcodes.services.projects import ProjectService
from shiftcodes.worker.github_handlers import (
    github_handlers,
    project_source,
    read_webhook_secret,
    signature_matches,
)
from shiftcodes.worker.handlers import build_handlers
from shiftcodes.worker.rpc import RpcServer
from tests.api_support import FakeClock, make_client, make_container, make_settings
from tests.github_support import SECRET, SHA_A, push_payload, sign, webhook_headers

ACTOR = Actor(ActorType.USER, "1")


class ScriptedHeads:
    """A ``BranchHeadReader`` that records calls and returns what the test scripted."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, GitTransport, str, bool]] = []
        self.result: BranchHead | None = BranchHead("development", SHA_A, "msg", "Ada")
        self.error: SourceError | None = None

    def head(
        self, repository: str, transport: GitTransport, branch: str, *, details: bool
    ) -> BranchHead | None:
        self.calls.append((repository, transport, branch, details))
        if self.error:
            raise self.error
        return self.result


@contextmanager
def worker(
    socket_dir: Path, engine: Engine, secret_file: Path | None, heads: ScriptedHeads
) -> Iterator[WorkerClient]:
    factory = create_session_factory(engine)
    methods = github_handlers(secret_file, heads, project_source(lambda: UnitOfWork(factory)))
    server = RpcServer(socket_dir / "w.sock", build_handlers(lambda: None, methods))  # type: ignore[arg-type,return-value]
    server.start()
    try:
        yield WorkerClient(socket_dir / "w.sock")
    finally:
        server.stop()


@pytest.fixture
def secret_file(tmp_path: Path) -> Path:
    path = tmp_path / "keys" / "webhook.secret"
    path.parent.mkdir()
    path.write_bytes(SECRET + b"\n")  # editors add a newline; it is not part of the secret
    return path


def test_signature_matches_is_exact() -> None:
    body = b"payload"
    assert signature_matches(SECRET, body, sign(body))
    assert not signature_matches(SECRET, body + b" ", sign(body))
    assert not signature_matches(SECRET, body, sign(body, b"other"))
    assert not signature_matches(SECRET, body, sign(body).upper())
    assert not signature_matches(SECRET, body, "")
    assert not signature_matches(SECRET, body, sign(body)[7:])


def test_secret_file_problems_are_configuration_errors(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="not configured"):
        read_webhook_secret(None)
    with pytest.raises(ConfigurationError, match="cannot be read"):
        read_webhook_secret(tmp_path / "missing")
    empty = tmp_path / "empty"
    empty.write_text("\n")
    with pytest.raises(ConfigurationError, match="empty"):
        read_webhook_secret(empty)


def test_verify_webhook_over_rpc(
    socket_dir: Path, engine: Engine, secret_file: Path, project: str
) -> None:
    body = push_payload()
    with worker(socket_dir, engine, secret_file, ScriptedHeads()) as client:
        assert client.verify_webhook(sign(body), body) is True
        assert client.verify_webhook(sign(body, b"x"), body) is False
        assert client.verify_webhook(sign(body), body + b"!") is False
        assert client.verify_webhook("sha256=" + "0" * 64, b"") is False


def test_a_large_body_fits_the_rpc_limit(
    socket_dir: Path, engine: Engine, secret_file: Path
) -> None:
    body = b"x" * (1024 * 1024)
    with worker(socket_dir, engine, secret_file, ScriptedHeads()) as client:
        assert client.verify_webhook(sign(body), body) is True


def test_a_missing_secret_is_a_configuration_error_not_a_pass(
    socket_dir: Path, engine: Engine
) -> None:
    with (
        worker(socket_dir, engine, None, ScriptedHeads()) as client,
        pytest.raises(ConfigurationError),
    ):
        client.verify_webhook(sign(b"x"), b"x")


def test_the_api_end_to_end_with_the_real_worker(
    tmp_path: Path, socket_dir: Path, engine: Engine, secret_file: Path, project: str
) -> None:
    with worker(socket_dir, engine, secret_file, ScriptedHeads()):
        settings = make_settings(tmp_path, socket_path=socket_dir / "w.sock")
        client: TestClient = make_client(make_container(settings, engine, FakeClock()))
        body = push_payload()
        assert (
            client.post(
                "/api/github/webhook", content=body, headers=webhook_headers(body)
            ).status_code
            == 202
        )
        bad = webhook_headers(body, signature=sign(body, b"nope"))
        assert client.post("/api/github/webhook", content=body, headers=bad).status_code == 401


def test_branch_head_uses_the_projects_repository_and_transport(
    socket_dir: Path, engine: Engine, secret_file: Path
) -> None:
    ProjectService(UnitOfWork(create_session_factory(engine))).create(
        "ssh-app", "SSH app", "octo/private", git_transport=GitTransport.SSH
    )
    heads = ScriptedHeads()
    with worker(socket_dir, engine, secret_file, heads) as client:
        head = client.branch_head("ssh-app", "development", ACTOR, details=True)
    assert (head.branch, head.commit_sha, head.message, head.author) == (
        "development",
        SHA_A,
        "msg",
        "Ada",
    )
    assert heads.calls == [("octo/private", GitTransport.SSH, "development", True)]


def test_branch_head_errors_map_to_domain_errors(
    socket_dir: Path, engine: Engine, secret_file: Path, project: str
) -> None:
    heads = ScriptedHeads()
    with worker(socket_dir, engine, secret_file, heads) as client:
        heads.result = None
        with pytest.raises(NotFoundError, match="branch not found"):
            client.branch_head(project, "gone", ACTOR)
        heads.error = SourceError("SSH access is not configured")
        with pytest.raises(SourceUnavailableError, match="SSH access is not configured"):
            client.branch_head(project, "development", ACTOR)
        with pytest.raises(NotFoundError, match="project not found"):
            client.branch_head("missing", "development", ACTOR)
        heads.error = None
        with pytest.raises(ValidationError, match="invalid branch name"):
            client.branch_head(project, "bad..branch", ACTOR)
