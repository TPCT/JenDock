"""Fetching the source code of a run at an exact commit."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Protocol

from shiftcodes.adapters.errors import SourceError

CLONE_TIMEOUT = 900.0
GIT_TIMEOUT = 120.0
# Files whose presence in a repository means a secret was committed; v1 refused them too.
FORBIDDEN_NAMES = frozenset({".env", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa"})
_CREDENTIALS_IN_URL = re.compile(r"(?<=://)[^/@\s]+@")


@dataclass(frozen=True, slots=True)
class CheckoutRequest:
    """What to fetch: the repository (as the project names it) and the pinned commit."""

    repository: str
    commit_sha: str


class SourceProvider(Protocol):
    """Puts the files of one commit into an empty directory."""

    def checkout(self, request: CheckoutRequest, destination: Path) -> None:
        """Clone and check out ``request.commit_sha``; raise ``SourceError`` on any problem."""


class UnavailableSource:
    """Placeholder until GitHub access exists (Task 6)."""

    def checkout(self, request: CheckoutRequest, destination: Path) -> None:  # noqa: ARG002
        """Always fails with an explanation."""
        raise SourceError("no source provider is configured")


def forbidden_tracked(paths: list[str]) -> list[str]:
    """The tracked paths that look like committed secrets."""
    return [p for p in paths if PurePosixPath(p).name in FORBIDDEN_NAMES]


class LocalGitSource:
    """Clones with git (a URL or a local path) and checks out the exact commit.

    ``resolve`` maps the repository name of a project to what ``git clone`` should read.
    """

    def __init__(self, resolve: Callable[[str], str], git: str = "git") -> None:
        self._resolve = resolve
        self._git = git

    def checkout(self, request: CheckoutRequest, destination: Path) -> None:
        """Clone without checkout, vet the tree, check out the commit and verify ``HEAD``."""
        url = self._resolve(request.repository)
        sha = request.commit_sha
        self._git_run(["clone", "--no-checkout", "--quiet", "--", url, str(destination)], None)
        listing = self._git_run(["ls-tree", "-r", "--name-only", "-z", sha], destination)
        bad = forbidden_tracked([p for p in listing.split("\0") if p])
        if bad:
            raise SourceError(
                f"refusing to build: the commit tracks secret-looking files: {', '.join(bad[:5])}"
            )
        self._git_run(["checkout", "--quiet", "--detach", sha], destination)
        head = self._git_run(["rev-parse", "HEAD"], destination).strip()
        if head != sha:
            raise SourceError(f"checked out {head[:12]} instead of the pinned {sha[:12]}")

    def _git_run(self, args: list[str], cwd: Path | None) -> str:
        argv = [self._git, "-c", "core.hooksPath=/dev/null", "-c", "protocol.version=2", *args]
        timeout = CLONE_TIMEOUT if args[0] == "clone" else GIT_TIMEOUT
        try:
            result = subprocess.run(  # noqa: S603 - explicit argv list, never a shell
                argv,
                cwd=cwd,
                env=_git_env(),
                capture_output=True,
                text=True,
                timeout=timeout,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise SourceError(f"git {args[0]} could not run: {error}") from error
        if result.returncode != 0:
            detail = _CREDENTIALS_IN_URL.sub("", result.stderr.strip())[:300]
            raise SourceError(f"git {args[0]} failed: {detail}")
        return result.stdout


def _git_env() -> dict[str, str]:
    env = {key: os.environ[key] for key in ("PATH", "LANG") if key in os.environ}
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "HOME": os.devnull,
            "GIT_ALLOW_PROTOCOL": "file:https:ssh",
        }
    )
    return env
