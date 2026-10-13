"""Recreating a single compose service with the ``docker compose`` plugin.

The argument prefix (``compose --env-file ... -p ... -f ...``) comes from ``ComposeProject``,
the one builder shared with the deployer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from shiftcodes.adapters.compose_project import ComposeProject
from shiftcodes.adapters.docker_commands import CommandRunner
from shiftcodes.adapters.errors import ContainerRuntimeError

COMPOSE_TIMEOUT = 180.0
MAX_ERROR_CHARS = 400


@dataclass(frozen=True, slots=True)
class ComposeTarget:
    """A compose project and the files that define it, in merge order.

    ``files`` are the repository's compose files followed by every managed override file (the
    deployer's image override last, so deployed images stay pinned).
    """

    project: str
    files: tuple[str, ...]
    env_files: tuple[str, ...] = ()


class ComposeEngine(Protocol):
    """Recreates one service of a compose project."""

    def recreate(self, target: ComposeTarget, service: str, *, start: bool) -> None:
        """Apply the files to ``service`` only; start it unless ``start`` is false."""


class DockerComposeEngine:
    """``docker compose up --no-deps --no-build --pull never`` for one service."""

    def __init__(self, docker: CommandRunner, timeout: float = COMPOSE_TIMEOUT) -> None:
        self._docker = docker
        self._timeout = timeout

    def recreate(self, target: ComposeTarget, service: str, *, start: bool) -> None:
        """Recreate ``service`` when its configuration changed.

        A running service is started again (``up --detach``). A stopped service is only
        created (``--no-start``) so that it stays stopped. Images are never built or pulled.
        """
        project = ComposeProject(self._docker, target.project, target.files, None, target.env_files)
        argv = [*project.base_command(), "up", "--no-deps", "--no-build", "--pull", "never"]
        argv += ["--detach"] if start else ["--no-start"]
        result = self._docker.capture([*argv, service], self._timeout)
        if result.returncode != 0:
            raise ContainerRuntimeError(_summary(result.stderr))


def _summary(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    text = " | ".join(lines[-3:]) if lines else "docker compose failed"
    return text[:MAX_ERROR_CHARS]
