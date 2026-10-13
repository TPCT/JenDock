"""Workspace directories and the git source."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from backend.adapters.errors import SourceError, WorkspaceError
from backend.adapters.sources import (
    CheckoutRequest,
    LocalGitSource,
    UnavailableSource,
    forbidden_tracked,
)
from backend.adapters.workspace import WorkspaceManager
from tests.git_support import commit, git, requires_git


def test_workspace_is_created_0750_and_replaced(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "ws")
    path = manager.create(7)
    assert path == tmp_path / "ws" / "7"
    assert path.stat().st_mode & 0o777 == 0o750
    (path / "old").write_text("x")
    again = manager.create(7)
    assert list(again.iterdir()) == []


def test_remove_deletes_read_only_trees(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "ws")
    path = manager.create(1)
    locked = path / "locked"
    locked.mkdir()
    (locked / "file").write_text("x")
    locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
    manager.remove(1)
    assert not path.exists()
    manager.remove(1)  # idempotent


def test_existing_ids_and_host_path(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "ws", host_root=Path("/host/ws"))
    manager.create(3)
    manager.create(10)
    (tmp_path / "ws" / "notes").mkdir()
    assert manager.existing_run_ids() == [3, 10]
    assert manager.host_path(3) == Path("/host/ws/3")
    assert manager.path(3) == tmp_path / "ws" / "3"


def test_disk_bound(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "ws", max_bytes=10_000)
    path = manager.create(1)
    (path / "small").write_bytes(b"x" * 100)
    manager.enforce_limit(path)
    (path / "big").write_bytes(b"x" * 50_000)
    with pytest.raises(WorkspaceError, match="over the limit"):
        manager.enforce_limit(path)


def test_size_ignores_symlinks(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path / "ws")
    path = manager.create(1)
    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"x" * 100_000)
    (path / "link").symlink_to(outside)
    assert manager.size(path) < 100_000


@requires_git
def test_checkout_pins_the_exact_commit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    first = commit(repo, {"app.txt": "v1\n", "Jenkinsfile": "x"})
    commit(repo, {"app.txt": "v2\n"})
    source = LocalGitSource(lambda _name: str(repo))
    destination = WorkspaceManager(tmp_path / "ws").create(1)
    source.checkout(CheckoutRequest("octo/demo", first), destination)
    assert (destination / "app.txt").read_text() == "v1\n"
    assert git(destination, "rev-parse", "HEAD") == first
    assert git(destination, "status", "--porcelain") == ""
    assert git(destination, "rev-parse", "--abbrev-ref", "HEAD") == "HEAD"  # detached


@requires_git
def test_checkout_resolves_the_repository_name_through_the_callback(tmp_path: Path) -> None:
    repo = tmp_path / "somewhere"
    sha = commit(repo, {"a": "1"})
    seen: list[str] = []

    def resolve(name: str) -> str:
        seen.append(name)
        return str(repo)

    destination = WorkspaceManager(tmp_path / "ws").create(1)
    LocalGitSource(resolve).checkout(CheckoutRequest("octo/demo", sha), destination)
    assert seen == ["octo/demo"]


@requires_git
@pytest.mark.parametrize("name", [".env", "config/.env", "id_rsa", "deploy/id_ed25519"])
def test_tracked_secret_files_are_refused_before_checkout(tmp_path: Path, name: str) -> None:
    repo = tmp_path / "repo"
    sha = commit(repo, {"ok.txt": "fine", name: "SECRET=1"})
    destination = WorkspaceManager(tmp_path / "ws").create(1)
    with pytest.raises(SourceError, match="secret-looking files"):
        LocalGitSource(lambda _n: str(repo)).checkout(CheckoutRequest("o/r", sha), destination)
    assert not (destination / "ok.txt").exists()  # nothing was written to the work tree


@requires_git
def test_env_example_is_allowed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    sha = commit(repo, {".env.example": "A=1"})
    destination = WorkspaceManager(tmp_path / "ws").create(1)
    LocalGitSource(lambda _n: str(repo)).checkout(CheckoutRequest("o/r", sha), destination)
    assert (destination / ".env.example").exists()


@requires_git
def test_unknown_commit_and_missing_repository(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    commit(repo, {"a": "1"})
    manager = WorkspaceManager(tmp_path / "ws")
    with pytest.raises(SourceError, match="git ls-tree failed"):
        LocalGitSource(lambda _n: str(repo)).checkout(
            CheckoutRequest("o/r", "f" * 40), manager.create(1)
        )
    with pytest.raises(SourceError, match="git clone failed"):
        LocalGitSource(lambda _n: str(tmp_path / "nope")).checkout(
            CheckoutRequest("o/r", "f" * 40), manager.create(2)
        )


def test_missing_git_binary_is_a_source_error(tmp_path: Path) -> None:
    source = LocalGitSource(lambda _n: "x", git=str(tmp_path / "no-git"))
    with pytest.raises(SourceError, match="could not run"):
        source.checkout(CheckoutRequest("o/r", "f" * 40), tmp_path)


def test_unavailable_source_and_forbidden_names() -> None:
    with pytest.raises(SourceError):
        UnavailableSource().checkout(CheckoutRequest("o/r", "a" * 40), Path("/nonexistent"))
    assert forbidden_tracked(["a/b.txt", "x/.env", ".env.local", "id_rsa.pub"]) == ["x/.env"]
