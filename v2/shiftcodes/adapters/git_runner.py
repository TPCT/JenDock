"""Running the git client: explicit argv, a minimal environment, bounded time."""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

from shiftcodes.adapters.errors import SourceError

CLONE_TIMEOUT = 900.0
GIT_TIMEOUT = 120.0
_CREDENTIALS_IN_URL = re.compile(r"(?<=://)[^/@\s]+@")
_LONG_COMMANDS = frozenset({"clone", "fetch"})


def git_environment(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment of every git process: no prompts, no user or system configuration."""
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
    env.update(extra or {})
    return env


class GitRunner:
    """Runs ``git`` with an argv list (never a shell) and raises ``SourceError`` on failure."""

    def __init__(self, git: str = "git", extra_env: Mapping[str, str] | None = None) -> None:
        self._git = git
        self._extra_env = dict(extra_env or {})

    def run(self, args: list[str], cwd: Path | None = None, timeout: float | None = None) -> str:
        """Run one git command and return its standard output.

        ``timeout`` overrides the default bound (long for clone and fetch, short otherwise).
        """
        argv = [self._git, "-c", "core.hooksPath=/dev/null", "-c", "protocol.version=2", *args]
        if timeout is None:
            timeout = CLONE_TIMEOUT if args[0] in _LONG_COMMANDS else GIT_TIMEOUT
        try:
            result = subprocess.run(  # noqa: S603 - explicit argv list, never a shell
                argv,
                cwd=cwd,
                env=git_environment(self._extra_env),
                capture_output=True,
                text=True,
                errors="replace",
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
