"""Reading the Jenkinsfile of a branch: git reader, worker RPC method and API client."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine

from shiftcodes.adapters.errors import SourceError
from shiftcodes.adapters.repo_files import GitFileReader
from shiftcodes.adapters.worker_client import RepositoryFile, WorkerClient
from shiftcodes.domain.actor import Actor
from shiftcodes.domain.enums import ActorType
from shiftcodes.domain.errors import NotFoundError, WorkerError
from shiftcodes.repositories.database import SessionFactory, UnitOfWork, create_session_factory
from shiftcodes.worker.pipeline_handlers import PipelineHandlers
from shiftcodes.worker.rpc import BadRequestError, RpcCall, RpcServer
from tests.conftest import DEV, PROD, SLUG
from tests.git_support import commit, git, requires_git

pytestmark = requires_git
ACTOR = Actor(ActorType.USER, "1")
TEXT = "pipeline { agent any }\n"


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    repo = tmp_path / "origin"
    commit(repo, {"Jenkinsfile": TEXT, "README.md": "x"})
    git(repo, "checkout", "-q", "-b", "development")
    commit(repo, {"Jenkinsfile": TEXT + "// development\n"})
    git(repo, "checkout", "-q", "-b", "production")
    git(repo, "rm", "-q", "Jenkinsfile")
    commit(repo, {"other.txt": "1"})
    return repo


@pytest.fixture
def reader(origin: Path) -> GitFileReader:
    return GitFileReader(lambda _repository: str(origin))


def test_reads_the_file_at_the_head_of_the_branch(reader: GitFileReader) -> None:
    assert reader.read_file("o/r", "main", "Jenkinsfile") == TEXT
    assert reader.read_file("o/r", "development", "Jenkinsfile") == TEXT + "// development\n"


def test_missing_branch_or_file_is_none_not_an_error(reader: GitFileReader) -> None:
    assert reader.read_file("o/r", "no-such-branch", "Jenkinsfile") is None
    assert reader.read_file("o/r", "production", "Jenkinsfile") is None
    assert reader.read_file("o/r", "main", "missing.txt") is None


def test_only_regular_files_count(origin: Path, reader: GitFileReader) -> None:
    git(origin, "checkout", "-q", "-b", "odd", "main")
    git(origin, "rm", "-q", "Jenkinsfile")
    (origin / "Jenkinsfile").symlink_to("README.md")
    git(origin, "add", "Jenkinsfile")
    commit(origin, {"dir/file": "1"})
    assert reader.read_file("o/r", "odd", "Jenkinsfile") is None  # a symlink is not a file
    assert reader.read_file("o/r", "odd", "dir") is None  # nor is a directory


def test_too_large_or_binary_files_are_refused(origin: Path) -> None:
    git(origin, "checkout", "-q", "-b", "big", "main")
    commit(origin, {"Jenkinsfile": "x" * 100})
    with pytest.raises(SourceError, match="larger than 50 bytes"):
        GitFileReader(lambda _r: str(origin), max_bytes=50).read_file("o/r", "big", "Jenkinsfile")
    git(origin, "checkout", "-q", "-b", "bin", "main")
    (origin / "Jenkinsfile").write_bytes(b"\xff\xfe\x00")
    git(origin, "commit", "-q", "--no-gpg-sign", "-am", "binary")
    with pytest.raises(SourceError, match="UTF-8"):
        GitFileReader(lambda _r: str(origin)).read_file("o/r", "bin", "Jenkinsfile")


def test_unreadable_repository_raises_source_error(tmp_path: Path) -> None:
    broken = GitFileReader(lambda _r: str(tmp_path / "nowhere"))
    with pytest.raises(SourceError):
        broken.read_file("o/r", "main", "Jenkinsfile")


def test_credentials_in_the_remote_url_never_reach_the_error(tmp_path: Path) -> None:
    secretive = GitFileReader(lambda _r: "https://user:hunter2@127.0.0.1:1/x.git")
    with pytest.raises(SourceError) as info:
        secretive.read_file("o/r", "main", "Jenkinsfile")
    assert "hunter2" not in str(info.value)


# --- worker method ---------------------------------------------------------------------------


@pytest.fixture
def factory(engine: Engine) -> SessionFactory:
    return create_session_factory(engine)


@pytest.fixture
def handlers(factory: SessionFactory, project: str, origin: Path) -> PipelineHandlers:
    reader = GitFileReader(lambda _repository: str(origin))
    return PipelineHandlers(lambda: UnitOfWork(factory), reader)


def _call(slug: str, env: str, actor: Actor | None = ACTOR) -> RpcCall:
    return RpcCall(actor, {"slug": slug, "env": env})


def test_handler_reads_the_branch_of_the_environment(handlers: PipelineHandlers) -> None:
    assert handlers.repository_file(_call(SLUG, DEV.value)) == {
        "reachable": True,
        "text": TEXT + "// development\n",
    }
    # the production branch has no Jenkinsfile: reachable, but nothing found
    assert handlers.repository_file(_call(SLUG, PROD.value)) == {"reachable": True, "text": None}


def test_handler_reports_an_unreachable_repository_without_details(
    factory: SessionFactory, project: str, tmp_path: Path
) -> None:
    reader = GitFileReader(lambda _r: str(tmp_path / "gone"))
    failing = PipelineHandlers(lambda: UnitOfWork(factory), reader)
    assert failing.repository_file(_call(SLUG, PROD.value)) == {"reachable": False, "text": None}


def test_handler_validates_its_input(handlers: PipelineHandlers) -> None:
    with pytest.raises(BadRequestError):
        handlers.repository_file(RpcCall(ACTOR, {"slug": SLUG}))
    with pytest.raises(BadRequestError):
        handlers.repository_file(_call(SLUG, "staging"))
    with pytest.raises(BadRequestError):
        handlers.repository_file(_call(SLUG, DEV.value, actor=None))
    with pytest.raises(NotFoundError):
        handlers.repository_file(_call("nope", DEV.value))


# --- client over the real socket -------------------------------------------------------------


@pytest.fixture
def served(handlers: PipelineHandlers, socket_dir: Path) -> Iterator[WorkerClient]:
    path = socket_dir / "w.sock"
    server = RpcServer(path, handlers.handlers())
    server.start()
    try:
        yield WorkerClient(path)
    finally:
        server.stop()


def test_client_returns_the_repository_file(served: WorkerClient) -> None:
    found = served.repository_jenkinsfile(SLUG, DEV, ACTOR)
    assert found == RepositoryFile(True, TEXT + "// development\n")


def test_client_maps_worker_errors(served: WorkerClient) -> None:
    with pytest.raises(NotFoundError):
        served.repository_jenkinsfile("nope", DEV, ACTOR)


def test_client_rejects_a_malformed_answer(socket_dir: Path) -> None:
    path = socket_dir / "w.sock"
    server = RpcServer(path, {"pipeline.repository_file": lambda _call: {"reachable": "yes"}})
    server.start()
    try:
        with pytest.raises(WorkerError):
            WorkerClient(path).repository_jenkinsfile(SLUG, DEV, ACTOR)
    finally:
        server.stop()
    assert RepositoryFile(True, None).text is None
