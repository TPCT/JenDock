"""Looking at a remote repository without cloning it: the head of a branch."""

from __future__ import annotations

import tempfile
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Protocol

from shiftcodes.adapters.errors import SourceError
from shiftcodes.adapters.git_access import GitAccess
from shiftcodes.adapters.git_runner import GitRunner
from shiftcodes.adapters.sources import CheckoutRequest, SourceProvider
from shiftcodes.domain.enums import GitTransport
from shiftcodes.domain.git import BranchHead
from shiftcodes.domain.values import BranchName

# An interactive request must not hang for the full clone timeout.
LOOKUP_TIMEOUT = 25.0
_FIELD_SEPARATOR = "\x00"


class BranchHeadReader(Protocol):
    """Resolves the head of a branch on the remote."""

    def head(
        self, repository: str, transport: GitTransport, branch: str, *, details: bool
    ) -> BranchHead | None:
        """Return the head, or ``None`` when the branch does not exist.

        With ``details`` the commit's message, author and date are fetched too.
        """


class BranchFileReader(Protocol):
    """Reads a file at the head of a branch (the pipeline editor's repository detection)."""

    def read_file(
        self, repository: str, transport: GitTransport, branch: str, path: str
    ) -> str | None:
        """The text of ``path`` at the head of ``branch``; ``None`` when the branch or the file
        does not exist; ``SourceError`` when the repository cannot be read."""


class HeadFileReader:
    """The one code path for reading a repository file: resolve the head with
    ``GitBranchHeads``, then read the file at that exact commit with the same
    ``SourceProvider`` runs use (so the deploy key or https choice of the project applies)."""

    def __init__(self, heads: BranchHeadReader, source: SourceProvider) -> None:
        self._heads = heads
        self._source = source

    def read_file(
        self, repository: str, transport: GitTransport, branch: str, path: str
    ) -> str | None:
        """See ``BranchFileReader``."""
        head = self._heads.head(repository, transport, branch, details=False)
        if head is None:
            return None
        return self._source.read_file(CheckoutRequest(repository, head.commit_sha, transport), path)


class GitBranchHeads:
    """``git ls-remote`` for the sha, plus a depth-1 fetch of that commit for its details."""

    def __init__(self, accesses: Mapping[GitTransport, GitAccess], git: str = "git") -> None:
        self._accesses = dict(accesses)
        self._git = git

    def head(
        self, repository: str, transport: GitTransport, branch: str, *, details: bool
    ) -> BranchHead | None:
        """Look the branch up; raises ``SourceError`` when the remote cannot be reached."""
        name = str(BranchName(branch))
        access = self._accesses.get(transport)
        if access is None:
            raise SourceError(f"no git access for transport {transport.value}")
        access.check()
        runner = GitRunner(self._git, access.env)
        url = access.resolve(repository)
        ref = f"refs/heads/{name}"
        listing = runner.run(["ls-remote", "--heads", "--", url, ref], timeout=LOOKUP_TIMEOUT)
        sha = _sha_of(listing, ref)
        if sha is None:
            return None
        if not details:
            return BranchHead(name, sha)
        return self._with_details(runner, url, name, sha)

    @staticmethod
    def _with_details(runner: GitRunner, url: str, branch: str, sha: str) -> BranchHead:
        with tempfile.TemporaryDirectory(prefix="sc-head-") as scratch:
            repo = Path(scratch)
            runner.run(["init", "--bare", "--quiet", str(repo)])
            fetch = ["fetch", "--quiet", "--depth", "1", "--no-tags", "--", url, sha]
            runner.run(fetch, repo, timeout=LOOKUP_TIMEOUT)
            fmt = "%an%x00%cI%x00%s"  # git expands %x00 itself; argv cannot hold a NUL
            raw = runner.run(["log", "-1", f"--format={fmt}", sha], repo)
        author, date, subject = [*raw.rstrip("\n").split(_FIELD_SEPARATOR), "", "", ""][:3]
        return BranchHead(branch, sha, subject, author, _parse_date(date))


def _sha_of(listing: str, ref: str) -> str | None:
    for line in listing.splitlines():
        sha, _, name = line.partition("\t")
        if name == ref and len(sha) == 40:
            return sha
    return None


def _parse_date(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
