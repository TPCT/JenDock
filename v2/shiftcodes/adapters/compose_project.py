"""One Docker Compose project as the deployer drives it.

Only the requested services are ever touched: every mutating command names its services and
uses ``--no-deps``; nothing here pulls, builds or removes orphans.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from shiftcodes.adapters.container_runtime import CancelSignal, ExecResult, LineSink
from shiftcodes.adapters.docker_commands import CommandRunner
from shiftcodes.adapters.errors import ContainerRuntimeError

SERVICE_LIST_TIMEOUT = 60.0


class ComposeProject:
    """``docker compose -p <name> -f <files...> [-f <managed override>]``."""

    def __init__(
        self,
        docker: CommandRunner,
        name: str,
        files: Sequence[str],
        override: Path | None = None,
    ) -> None:
        self._docker = docker
        self._name = name
        self._files = tuple(files)
        self._override = override

    def _prefix(self, *, with_override: bool) -> list[str]:
        args = ["compose", "-p", self._name]
        for file in self._files:
            args += ["-f", file]
        if with_override and self._override is not None:
            args += ["-f", str(self._override)]
        return args

    def defined_services(self) -> set[str]:
        """Services defined by the environment's own compose files (not the managed override)."""
        result = self._docker.capture(
            [*self._prefix(with_override=False), "config", "--services"],
            SERVICE_LIST_TIMEOUT,
        )
        if result.returncode != 0:
            raise ContainerRuntimeError(f"docker compose config failed: {_brief(result.stderr)}")
        return {line.strip() for line in result.stdout.splitlines() if line.strip()}

    def container_ids(self, service: str) -> list[str]:
        """Ids of the containers of ``service`` in any state."""
        result = self._docker.capture(
            [*self._prefix(with_override=False), "ps", "--all", "--quiet", service]
        )
        if result.returncode != 0:
            raise ContainerRuntimeError(f"docker compose ps failed: {_brief(result.stderr)}")
        return result.stdout.split()

    def up(
        self,
        services: Sequence[str],
        sink: LineSink,
        timeout: float,
        cancel: CancelSignal | None = None,
    ) -> ExecResult:
        """Create or recreate exactly ``services``: no dependencies, no pull, no build."""
        args = [
            *self._prefix(with_override=True),
            "up",
            "--detach",
            "--no-deps",
            "--no-build",
            "--pull",
            "never",
            *services,
        ]
        return self._docker.stream(args, sink, timeout=timeout, cancel=cancel)

    def remove(self, service: str, sink: LineSink, timeout: float) -> ExecResult:
        """Stop and remove the containers of one service (and nothing else)."""
        args = [*self._prefix(with_override=False), "rm", "--stop", "--force", service]
        return self._docker.stream(args, sink, timeout=timeout)


def _brief(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else "no output"
