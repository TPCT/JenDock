"""Running ``apachectl`` with a fixed argv; nothing from a request ever reaches the command."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

MAX_OUTPUT_CHARS = 2000
SAFE_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"}


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Outcome of one apachectl call."""

    ok: bool
    output: str


class ApacheControl:
    """``configtest`` and ``graceful`` through the administrator-configured apachectl."""

    def __init__(self, argv: tuple[str, ...], timeout: float) -> None:
        self._argv = argv
        self._timeout = timeout

    def configtest(self) -> CommandResult:
        """Check the complete Apache configuration."""
        return self._run("configtest")

    def reload(self) -> CommandResult:
        """Graceful reload: running requests finish, no downtime."""
        return self._run("graceful")

    def _run(self, action: str) -> CommandResult:
        try:
            completed = subprocess.run(  # noqa: S603 - fixed argv from the admin config
                [*self._argv, action],
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
                env=SAFE_ENV,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            return CommandResult(False, f"{action} could not run: {error}"[:MAX_OUTPUT_CHARS])
        output = (completed.stderr + completed.stdout).strip()
        return CommandResult(completed.returncode == 0, output[-MAX_OUTPUT_CHARS:])
