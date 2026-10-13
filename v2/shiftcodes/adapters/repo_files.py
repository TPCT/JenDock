"""Reading one file of a repository at the head of a branch, without a full clone."""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from shiftcodes.adapters.errors import SourceError
from shiftcodes.adapters.sources import _CREDENTIALS_IN_URL, _git_env

FETCH_TIMEOUT = 25.0
GIT_TIMEOUT = 15.0
_MISSING_REF = "couldn't find remote ref"
_REGULAR_BLOB_MODES = frozenset({"100644", "100755"})


class RepositoryFileReader(Protocol):
    """Reads a file from the head of a branch."""

    def read_file(self, repository: str, branch: str, path: str) -> str | None:
        """The UTF-8 text of ``path`` at the head of ``branch``.

        ``None`` when the branch or the file does not exist (or is not a regular file); raises
        ``SourceError`` when the repository cannot be read or the file is too large or binary.
        """


class GitFileReader:
    """Fetches only the head commit of the branch (no file contents) into a temporary
    repository and reads the single blob from it. ``resolve`` maps the repository name of a
    project to what ``git fetch`` should read, exactly like ``LocalGitSource``.
    """

    def __init__(
        self, resolve: Callable[[str], str], git: str = "git", max_bytes: int = 256 * 1024
    ) -> None:
        self._resolve = resolve
        self._git = git
        self._max_bytes = max_bytes

    def read_file(self, repository: str, branch: str, path: str) -> str | None:
        """See ``RepositoryFileReader``."""
        url = self._resolve(repository)
        with tempfile.TemporaryDirectory(prefix="sc-peek-") as directory:
            work = Path(directory)
            self._prepare(work, url)
            fetch = ["fetch", "--quiet", "--depth", "1", "--no-tags", "origin"]
            fetched = self._git_run(
                [*fetch, f"refs/heads/{branch}"], work, FETCH_TIMEOUT, allow_missing=True
            )
            if fetched is None:
                return None
            return self._read_blob(work, path)

    def _prepare(self, work: Path, url: str) -> None:
        """An empty repository whose ``origin`` is a promisor remote: only the commit and its
        trees are fetched, and the one blob that is read later is fetched on demand."""
        self._git_run(["init", "--quiet"], work, GIT_TIMEOUT)
        self._git_run(["remote", "add", "--", "origin", url], work, GIT_TIMEOUT)
        self._git_run(["config", "remote.origin.promisor", "true"], work, GIT_TIMEOUT)
        self._git_run(
            ["config", "remote.origin.partialclonefilter", "blob:none"], work, GIT_TIMEOUT
        )

    def _read_blob(self, work: Path, path: str) -> str | None:
        listing = self._git_run(["ls-tree", "-z", "FETCH_HEAD", "--", path], work, GIT_TIMEOUT)
        mode = (listing or "").split(" ", 1)[0]
        if mode not in _REGULAR_BLOB_MODES:
            return None
        size = self._git_run(["cat-file", "-s", f"FETCH_HEAD:{path}"], work, FETCH_TIMEOUT) or "0"
        if int(size.strip()) > self._max_bytes:
            raise SourceError(f"{path} is larger than {self._max_bytes} bytes")
        content = self._git_bytes(["cat-file", "blob", f"FETCH_HEAD:{path}"], work)
        try:
            return content.decode("utf-8")
        except UnicodeDecodeError as error:
            raise SourceError(f"{path} is not valid UTF-8 text") from error

    def _git_run(
        self, args: list[str], cwd: Path, timeout: float, *, allow_missing: bool = False
    ) -> str | None:
        """Text output of git; ``None`` only for a missing remote ref when ``allow_missing``."""
        result = self._run(args, cwd, timeout)
        if result.returncode == 0:
            return result.stdout.decode("utf-8", errors="replace")
        detail = _CREDENTIALS_IN_URL.sub("", result.stderr.decode("utf-8", errors="replace"))
        if allow_missing and _MISSING_REF in detail:
            return None
        raise SourceError(f"git {args[0]} failed: {detail.strip()[:300]}")

    def _git_bytes(self, args: list[str], cwd: Path) -> bytes:
        result = self._run(args, cwd, FETCH_TIMEOUT)
        if result.returncode != 0:
            raise SourceError(f"git {args[0]} failed")
        return result.stdout

    def _run(
        self, args: list[str], cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[bytes]:
        argv = [self._git, "-c", "core.hooksPath=/dev/null", "-c", "protocol.version=2", *args]
        try:
            return subprocess.run(  # noqa: S603 - explicit argv list, never a shell
                argv,
                cwd=cwd,
                env=_git_env(),
                capture_output=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise SourceError(f"git {args[0]} could not run: {error}") from error
