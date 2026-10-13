"""Fetching the source code of a run at an exact commit."""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from shiftcodes.adapters.errors import SourceError
from shiftcodes.adapters.git_runner import GitRunner
from shiftcodes.domain.enums import GitTransport

# Files whose presence in a repository means a secret was committed; v1 refused them too.
FORBIDDEN_NAMES = frozenset({".env", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"})
# Same bound as the Jenkinsfile parser (256 KiB), so a bigger file fails early and clearly.
MAX_REPOSITORY_FILE_BYTES = 256 * 1024
# An interactive read (or the start of a run) must not hang for the full clone timeout.
READ_FETCH_TIMEOUT = 30.0
_REGULAR_FILE_MODES = frozenset({"100644", "100755"})


@dataclass(frozen=True, slots=True)
class CheckoutRequest:
    """What to fetch: the repository (as the project names it) and the pinned commit.

    ``transport`` says how the project reaches the repository (deploy key or public HTTPS).
    """

    repository: str
    commit_sha: str
    transport: GitTransport = GitTransport.HTTPS


class SourceProvider(Protocol):
    """Puts the files of one commit into an empty directory."""

    def checkout(self, request: CheckoutRequest, destination: Path) -> None:
        """Clone and check out ``request.commit_sha``; raise ``SourceError`` on any problem."""

    def read_file(self, request: CheckoutRequest, path: str) -> str | None:
        """Return the text of a regular file at the pinned commit, or ``None`` when absent.

        Raise ``SourceError`` when the commit cannot be fetched or the path is not a
        readable regular file (a symlink, a directory or too large).
        """


class UnavailableSource:
    """Placeholder for a deployment without any source access."""

    def checkout(self, request: CheckoutRequest, destination: Path) -> None:  # noqa: ARG002
        """Always fails with an explanation."""
        raise SourceError("no source provider is configured")

    def read_file(self, request: CheckoutRequest, path: str) -> str | None:  # noqa: ARG002
        """Always fails with an explanation."""
        raise SourceError("no source provider is configured")


def forbidden_tracked(paths: list[str]) -> list[str]:
    """The tracked paths that look like committed secrets."""
    return [p for p in paths if PurePosixPath(p).name in FORBIDDEN_NAMES]


class LocalGitSource:
    """Clones with git (a URL or a local path) and checks out the exact commit.

    ``resolve`` maps the repository name of a project to what ``git clone`` should read;
    ``extra_env`` adds variables to the git process (for example ``GIT_SSH_COMMAND``).
    """

    def __init__(
        self,
        resolve: Callable[[str], str],
        git: str = "git",
        extra_env: Mapping[str, str] | None = None,
    ) -> None:
        self._resolve = resolve
        self._git = GitRunner(git, extra_env)

    def checkout(self, request: CheckoutRequest, destination: Path) -> None:
        """Clone without checkout, vet the tree, check out the commit and verify ``HEAD``."""
        url = self._resolve(request.repository)
        sha = request.commit_sha
        self._git.run(["clone", "--no-checkout", "--quiet", "--", url, str(destination)])
        listing = self._git.run(["ls-tree", "-r", "--name-only", "-z", sha], destination)
        bad = forbidden_tracked([p for p in listing.split("\0") if p])
        if bad:
            raise SourceError(
                f"refusing to build: the commit tracks secret-looking files: {', '.join(bad[:5])}"
            )
        self._git.run(["checkout", "--quiet", "--detach", sha], destination)
        head = self._git.run(["rev-parse", "HEAD"], destination).strip()
        if head != sha:
            raise SourceError(f"checked out {head[:12]} instead of the pinned {sha[:12]}")

    def read_file(self, request: CheckoutRequest, path: str) -> str | None:
        """Fetch only the pinned commit (depth 1, no checkout) and read one file from it."""
        url = self._resolve(request.repository)
        sha = request.commit_sha
        with tempfile.TemporaryDirectory(prefix="sc-read-") as scratch:
            repo = Path(scratch)
            self._git.run(["init", "--bare", "--quiet", str(repo)])
            self._git.run(
                ["fetch", "--quiet", "--depth", "1", "--no-tags", "--", url, sha],
                repo,
                timeout=READ_FETCH_TIMEOUT,
            )
            return self._read_blob(repo, sha, path)

    def _read_blob(self, repo: Path, sha: str, path: str) -> str | None:
        entry = self._git.run(["ls-tree", "-z", sha, "--", path], repo).rstrip("\0")
        if not entry:
            return None
        mode, _kind, rest = entry.split(" ", 2)
        if mode not in _REGULAR_FILE_MODES:
            raise SourceError(f"{path} is not a regular file in the repository")
        object_id = rest.split("\t", 1)[0]
        size = int(self._git.run(["cat-file", "-s", object_id], repo).strip())
        if size > MAX_REPOSITORY_FILE_BYTES:
            raise SourceError(f"{path} is larger than {MAX_REPOSITORY_FILE_BYTES // 1024} KiB")
        return self._git.run(["cat-file", "blob", object_id], repo)
