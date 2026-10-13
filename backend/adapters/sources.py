"""Fetching the source code of a run at an exact commit."""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from backend.adapters.errors import SourceError, SourceTimeoutError
from backend.adapters.git_runner import GitRunner
from backend.domain.enums import GitTransport

# Files whose presence in a repository means a secret was committed.
FORBIDDEN_NAMES = frozenset({".env", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"})
# Same bound as the Jenkinsfile parser (256 KiB), so a bigger file fails early and clearly.
MAX_REPOSITORY_FILE_BYTES = 256 * 1024
# An interactive read (or the start of a run) must not hang for the full clone timeout; a read
# that times out is tried once more, since a slow moment of the network is not a bad repository.
READ_FETCH_TIMEOUT = 45.0
READ_FETCH_ATTEMPTS = 2
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
    ``env_for`` gives the variables added to the git process for a repository (for example
    ``GIT_SSH_COMMAND``).
    """

    def __init__(
        self,
        resolve: Callable[[str], str],
        git: str = "git",
        env_for: Callable[[str], Mapping[str, str]] | None = None,
    ) -> None:
        self._resolve = resolve
        self._git_binary = git
        self._env_for = env_for or (lambda _repository: {})

    def _runner(self, repository: str) -> GitRunner:
        return GitRunner(self._git_binary, self._env_for(repository))

    def checkout(self, request: CheckoutRequest, destination: Path) -> None:
        """Fetch the commit alone, vet its tree, check it out and verify ``HEAD``."""
        url = self._resolve(request.repository)
        sha = request.commit_sha
        git = self._runner(request.repository)
        # Only the pinned commit is fetched (depth 1): a build needs its files, not the history,
        # and a repository whose history carries large assets would otherwise take many minutes.
        git.run(["init", "--quiet", str(destination)])
        git.run(["remote", "add", "origin", url], destination)
        git.run(["fetch", "--quiet", "--depth", "1", "--no-tags", "origin", sha], destination)
        listing = git.run(["ls-tree", "-r", "--name-only", "-z", sha], destination)
        bad = forbidden_tracked([p for p in listing.split("\0") if p])
        if bad:
            raise SourceError(
                f"refusing to build: the commit tracks secret-looking files: {', '.join(bad[:5])}"
            )
        git.run(["checkout", "--quiet", "--detach", sha], destination)
        head = git.run(["rev-parse", "HEAD"], destination).strip()
        if head != sha:
            raise SourceError(f"checked out {head[:12]} instead of the pinned {sha[:12]}")
        make_world_readable(destination)

    def read_file(self, request: CheckoutRequest, path: str) -> str | None:
        """Read one file at the pinned commit without downloading the rest of it.

        A partial fetch (``--filter=blob:none``, depth 1) brings only the commit and its trees;
        the one file's content is then fetched on demand from the same remote, which is marked as
        a promisor. Reading a Jenkinsfile of a large repository over a slow link costs a few
        hundred KiB instead of the whole snapshot. A server without filter support simply sends
        everything, so the result is the same.
        """
        url = self._resolve(request.repository)
        sha = request.commit_sha
        git = self._runner(request.repository)
        with tempfile.TemporaryDirectory(prefix="jd-read-") as scratch:
            repo = Path(scratch)
            git.run(["init", "--bare", "--quiet", str(repo)])
            git.run(["remote", "add", "origin", url], repo)
            git.run(["config", "remote.origin.promisor", "true"], repo)
            git.run(["config", "remote.origin.partialclonefilter", "blob:none"], repo)
            self._fetch_commit(git, repo, sha)
            return self._read_blob(git, repo, sha, path)

    @staticmethod
    def _fetch_commit(git: GitRunner, repo: Path, sha: str) -> None:
        """Fetch one commit without file contents, trying again once when it only timed out."""
        for attempt in range(1, READ_FETCH_ATTEMPTS + 1):
            try:
                git.run(
                    [
                        "fetch",
                        "--quiet",
                        "--depth",
                        "1",
                        "--no-tags",
                        "--filter=blob:none",
                        "origin",
                        sha,
                    ],
                    repo,
                    timeout=READ_FETCH_TIMEOUT,
                )
            except SourceTimeoutError:
                if attempt == READ_FETCH_ATTEMPTS:
                    raise
            else:
                return

    @staticmethod
    def _read_blob(git: GitRunner, repo: Path, sha: str, path: str) -> str | None:
        entry = git.run(["ls-tree", "-z", sha, "--", path], repo).rstrip("\0")
        if not entry:
            return None
        mode, _kind, rest = entry.split(" ", 2)
        if mode not in _REGULAR_FILE_MODES:
            raise SourceError(f"{path} is not a regular file in the repository")
        object_id = rest.split("\t", 1)[0]
        size = int(git.run(["cat-file", "-s", object_id], repo).strip())
        if size > MAX_REPOSITORY_FILE_BYTES:
            raise SourceError(f"{path} is larger than {MAX_REPOSITORY_FILE_BYTES // 1024} KiB")
        return git.run(["cat-file", "blob", object_id], repo)


def make_world_readable(root: Path) -> None:
    """Give the checked-out tree the modes of a normal clone (umask 022).

    The worker runs with umask 0007 for its shared data volume, which git inherits; ``COPY``
    in a Dockerfile keeps those modes, so an image whose process runs as an unprivileged user
    could not read its own files. Directories become 0755-style, files gain read for everyone
    and keep execute only where the owner had it. ``.git`` is left alone; symlinks are skipped.
    """
    for directory, subdirs, files in os.walk(root):
        if ".git" in subdirs:
            subdirs.remove(".git")
        _add_bits(Path(directory), stat.S_IRGRP | stat.S_IROTH | stat.S_IXGRP | stat.S_IXOTH)
        for name in files:
            path = Path(directory, name)
            if path.is_symlink():
                continue
            mode = path.stat().st_mode
            extra = stat.S_IRGRP | stat.S_IROTH
            if mode & stat.S_IXUSR:
                extra |= stat.S_IXGRP | stat.S_IXOTH
            _add_bits(path, extra)


def _add_bits(path: Path, bits: int) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & bits != bits:
        path.chmod(mode | bits)
