"""Reading one file at a commit and resolving branch heads, against throwaway local repositories."""

from __future__ import annotations

from datetime import UTC
from pathlib import Path

import pytest

from shiftcodes.adapters.errors import SourceError
from shiftcodes.adapters.git_access import GitAccess, unavailable_ssh_access
from shiftcodes.adapters.git_remote import GitBranchHeads
from shiftcodes.adapters.sources import (
    MAX_REPOSITORY_FILE_BYTES,
    CheckoutRequest,
    LocalGitSource,
    UnavailableSource,
)
from shiftcodes.domain.enums import GitTransport
from shiftcodes.domain.errors import ValidationError
from tests.git_support import commit, git, requires_git

pytestmark = requires_git


def _local(repo: Path) -> GitAccess:
    return GitAccess(lambda _name: str(repo), {}, lambda: None)


def _heads(repo: Path) -> GitBranchHeads:
    return GitBranchHeads({GitTransport.HTTPS: _local(repo)})


# --- read_file -------------------------------------------------------------------------------


def test_read_file_returns_the_text_at_the_pinned_commit(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    first = commit(repo, {"Jenkinsfile": "pipeline { v1 }\n", "a.txt": "1"})
    second = commit(repo, {"Jenkinsfile": "pipeline { v2 }\n"})
    source = LocalGitSource(lambda _n: str(repo))
    assert source.read_file(CheckoutRequest("o/r", first), "Jenkinsfile") == "pipeline { v1 }\n"
    assert source.read_file(CheckoutRequest("o/r", second), "Jenkinsfile") == "pipeline { v2 }\n"


def test_read_file_reports_absence_as_none(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    sha = commit(repo, {"a.txt": "1", "sub/Jenkinsfile": "nested does not count"})
    source = LocalGitSource(lambda _n: str(repo))
    assert source.read_file(CheckoutRequest("o/r", sha), "Jenkinsfile") is None


def test_read_file_does_not_touch_the_workspace_or_clone(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    sha = commit(repo, {"Jenkinsfile": "x"})
    before = set(tmp_path.iterdir())
    LocalGitSource(lambda _n: str(repo)).read_file(CheckoutRequest("o/r", sha), "Jenkinsfile")
    assert set(tmp_path.iterdir()) == before


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_a_jenkinsfile_that_is_not_a_regular_file_is_an_error(tmp_path: Path, kind: str) -> None:
    repo = tmp_path / "repo"
    sha = commit(repo, {"real": "pipeline {}"})
    if kind == "symlink":
        (repo / "Jenkinsfile").symlink_to("/etc/passwd")
    else:
        (repo / "Jenkinsfile").mkdir()
        (repo / "Jenkinsfile" / "x").write_text("x")
    git(repo, "add", "--all", "--force")
    git(repo, "commit", "--quiet", "--no-gpg-sign", "-m", kind)
    sha = git(repo, "rev-parse", "HEAD")
    with pytest.raises(SourceError, match="not a regular file"):
        LocalGitSource(lambda _n: str(repo)).read_file(CheckoutRequest("o/r", sha), "Jenkinsfile")


def test_an_oversized_jenkinsfile_is_an_error(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    sha = commit(repo, {"Jenkinsfile": "x" * (MAX_REPOSITORY_FILE_BYTES + 1)})
    with pytest.raises(SourceError, match="larger than"):
        LocalGitSource(lambda _n: str(repo)).read_file(CheckoutRequest("o/r", sha), "Jenkinsfile")


def test_read_file_errors_for_an_unknown_commit_or_repository(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    commit(repo, {"Jenkinsfile": "x"})
    with pytest.raises(SourceError, match="git fetch failed"):
        LocalGitSource(lambda _n: str(repo)).read_file(
            CheckoutRequest("o/r", "f" * 40), "Jenkinsfile"
        )
    with pytest.raises(SourceError, match="git fetch failed"):
        LocalGitSource(lambda _n: str(tmp_path / "nope")).read_file(
            CheckoutRequest("o/r", "f" * 40), "Jenkinsfile"
        )


def test_the_unavailable_source_cannot_read_either() -> None:
    with pytest.raises(SourceError):
        UnavailableSource().read_file(CheckoutRequest("o/r", "a" * 40), "Jenkinsfile")


# --- branch heads ----------------------------------------------------------------------------


def test_head_without_details_is_just_the_sha(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    commit(repo, {"a": "1"})
    sha = commit(repo, {"a": "2"}, "second")
    head = _heads(repo).head("o/r", GitTransport.HTTPS, "main", details=False)
    assert head is not None
    assert (head.branch, head.commit_sha, head.message, head.author) == ("main", sha, "", "")
    assert head.committed_at is None


def test_head_with_details_has_message_author_and_date(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    commit(repo, {"a": "1"})
    sha = commit(repo, {"a": "2"}, "Fix the login redirect")
    head = _heads(repo).head("o/r", GitTransport.HTTPS, "main", details=True)
    assert head is not None
    assert head.commit_sha == sha
    assert head.message == "Fix the login redirect"
    assert head.author == "Test"
    assert head.committed_at is not None
    assert head.committed_at.astimezone(UTC).year >= 2026


def test_a_missing_branch_is_none_and_names_are_matched_exactly(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    main = commit(repo, {"a": "1"})
    git(repo, "checkout", "--quiet", "-b", "feature/main")
    other = commit(repo, {"a": "2"})
    git(repo, "checkout", "--quiet", "main")
    heads = _heads(repo)
    found = heads.head("o/r", GitTransport.HTTPS, "main", details=False)
    nested = heads.head("o/r", GitTransport.HTTPS, "feature/main", details=False)
    assert found is not None
    assert nested is not None
    assert (found.commit_sha, nested.commit_sha) == (main, other)
    assert heads.head("o/r", GitTransport.HTTPS, "mai", details=False) is None
    assert heads.head("o/r", GitTransport.HTTPS, "gone", details=False) is None


def test_head_rejects_unsafe_branch_names_before_running_git(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    commit(repo, {"a": "1"})
    with pytest.raises(ValidationError):
        _heads(repo).head("o/r", GitTransport.HTTPS, "--upload-pack=evil", details=False)


def test_head_over_a_transport_that_is_not_usable(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    commit(repo, {"a": "1"})
    heads = GitBranchHeads({GitTransport.SSH: unavailable_ssh_access()})
    with pytest.raises(SourceError, match="SSH access is not configured"):
        heads.head("o/r", GitTransport.SSH, "main", details=False)
    with pytest.raises(SourceError, match="no git access for transport https"):
        heads.head("o/r", GitTransport.HTTPS, "main", details=False)


def test_head_of_an_unreachable_repository_is_a_source_error(tmp_path: Path) -> None:
    heads = _heads(tmp_path / "does-not-exist")
    with pytest.raises(SourceError, match="git ls-remote failed"):
        heads.head("o/r", GitTransport.HTTPS, "main", details=False)
