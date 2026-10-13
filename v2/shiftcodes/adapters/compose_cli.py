"""Recreating a single compose service with the ``docker compose`` plugin."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from shiftcodes.adapters.docker_client import DockerClient
from shiftcodes.adapters.errors import ContainerRuntimeError

COMPOSE_TIMEOUT = 180.0
MAX_ERROR_CHARS = 400


@dataclass(frozen=True, slots=True)
class ComposeTarget:
    """A compose project and the files that define it, in merge order.

    ``files`` are the repository's compose files followed by every managed override file.
    """

    project: str
    files: tuple[str, ...]


class ComposeEngine(Protocol):
    """Recreates one service of a compose project."""

    def recreate(self, target: ComposeTarget, service: str, *, start: bool) -> None:
        """Apply the files to ``service`` only; start it unless ``start`` is false."""


class DockerComposeEngine:
    """``docker compose up --no-deps --no-build`` for one service."""

    def __init__(self, client: DockerClient, timeout: float = COMPOSE_TIMEOUT) -> None:
        self._client = client
        self._timeout = timeout

    def recreate(self, target: ComposeTarget, service: str, *, start: bool) -> None:
        """Recreate ``service`` when its configuration changed.

        A running service is started again (``up -d``). A stopped service is only created
        (``--no-start``) so that it stays stopped. Images are never built or pulled.
        """
        argv = ["compose", "--project-name", target.project]
        for path in target.files:
            argv += ["--file", path]
        argv += ["up", "--no-deps", "--no-build", "--pull", "never"]
        argv += ["--detach"] if start else ["--no-start"]
        result = self._client.run([*argv, service], timeout=self._timeout)
        if result.returncode != 0:
            raise ContainerRuntimeError(_summary(result.stderr))


def compose_files(base: Sequence[str], overrides: Sequence[str]) -> tuple[str, ...]:
    """Repository files first, managed override files after them."""
    return (*base, *overrides)


def _summary(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    text = " | ".join(lines[-3:]) if lines else "docker compose failed"
    return text[:MAX_ERROR_CHARS]
