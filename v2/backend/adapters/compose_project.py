"""One Docker Compose project as the deployer drives it.

Only the requested services are ever touched: every mutating command names its services and
uses ``--no-deps``; nothing here pulls, builds or removes orphans.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from backend.adapters.container_runtime import CancelSignal, ExecResult, LineSink
from backend.adapters.docker_commands import CommandRunner
from backend.adapters.errors import ContainerRuntimeError, DeployError

SERVICE_LIST_TIMEOUT = 60.0


class ComposeProject:
    """``docker compose -p <name> -f <files...> [-f <managed override>]``."""

    def __init__(
        self,
        docker: CommandRunner,
        name: str,
        files: Sequence[str],
        override: Path | None = None,
        env_files: Sequence[str] = (),
    ) -> None:
        self._docker = docker
        self._name = name
        self._files = tuple(files)
        self._override = override
        self._env_files = tuple(env_files)

    def base_command(self, *, with_override: bool = True) -> list[str]:
        """``compose [--env-file F]... -p <name> -f <file>... [-f <override>]``: the one place
        where the argument prefix of every compose invocation is built."""
        args = ["compose"]
        for env_file in self._env_files:
            args += ["--env-file", env_file]
        args += ["-p", self._name]
        for file in self._files:
            args += ["-f", file]
        if with_override and self._override is not None:
            args += ["-f", str(self._override)]
        return args

    def defined_services(self) -> set[str]:
        """Services defined by the environment's own compose files (not the managed override)."""
        result = self._docker.capture(
            [*self.base_command(with_override=False), "config", "--services"],
            SERVICE_LIST_TIMEOUT,
        )
        if result.returncode != 0:
            raise ContainerRuntimeError(f"docker compose config failed: {_brief(result.stderr)}")
        return {line.strip() for line in result.stdout.splitlines() if line.strip()}

    def container_ids(self, service: str) -> list[str]:
        """Ids of the containers of ``service`` in any state."""
        result = self._docker.capture(
            [*self.base_command(with_override=False), "ps", "--all", "--quiet", service]
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
            *self.base_command(with_override=True),
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
        args = [*self.base_command(with_override=False), "rm", "--stop", "--force", service]
        return self._docker.stream(args, sink, timeout=timeout)


def ensure_readable(files: Sequence[str], env_files: Sequence[str]) -> None:
    """Raise ``DeployError`` naming the first compose or env file the worker cannot read (the
    host projects directory must be mounted into the worker at the same absolute path)."""
    for kind, paths in (("compose file", files), ("compose env file", env_files)):
        for raw in paths:
            if not os.access(raw, os.R_OK) or not Path(raw).is_file():
                raise DeployError(
                    f"{kind} {raw} is not readable by the worker; check that the host directory "
                    "is mounted at the same path (COMPOSE_PROJECTS_DIR)"
                )


def _brief(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1] if lines else "no output"
