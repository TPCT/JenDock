"""Running docker (and ``docker compose``) command lines for deployment work.

Everything is an explicit argument list, never a shell. Secrets never travel here: the
arguments are image references, service names and file paths only.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from typing import Protocol

from backend.adapters.container_runtime import CancelSignal, ExecResult, LineSink
from backend.adapters.docker_process import stream_process
from backend.adapters.errors import ContainerRuntimeError

QUERY_TIMEOUT = 60.0
_BASE_ENV_KEYS = ("PATH", "HOME", "LANG", "DOCKER_CONFIG", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY")


class CommandRunner(Protocol):
    """Runs docker commands; the real implementation is ``DockerCommands``."""

    def capture(
        self, args: Sequence[str], timeout: float = QUERY_TIMEOUT
    ) -> subprocess.CompletedProcess[str]:
        """Run ``docker <args>`` and capture its output."""

    def stream(
        self,
        args: Sequence[str],
        sink: LineSink,
        *,
        timeout: float | None,
        cancel: CancelSignal | None = None,
    ) -> ExecResult:
        """Run ``docker <args>`` streaming its output line by line."""


class DockerCommands:
    """``CommandRunner`` over the docker command line client."""

    def __init__(self, binary: str = "docker", docker_host: str | None = None) -> None:
        self._binary = binary
        self._env = {key: os.environ[key] for key in _BASE_ENV_KEYS if key in os.environ}
        host = docker_host or os.environ.get("DOCKER_HOST")
        if host:
            self._env["DOCKER_HOST"] = host

    def capture(
        self, args: Sequence[str], timeout: float = QUERY_TIMEOUT
    ) -> subprocess.CompletedProcess[str]:
        """Run ``docker <args>`` and capture its output."""
        try:
            return subprocess.run(  # noqa: S603 - explicit argv list, never a shell
                [self._binary, *args],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self._env,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ContainerRuntimeError(f"docker is not usable: {error}") from error

    def stream(
        self,
        args: Sequence[str],
        sink: LineSink,
        *,
        timeout: float | None,
        cancel: CancelSignal | None = None,
    ) -> ExecResult:
        """Run ``docker <args>`` streaming its output line by line."""
        try:
            return stream_process(
                [self._binary, *args], self._env, sink, timeout=timeout, cancel=cancel
            )
        except OSError as error:
            raise ContainerRuntimeError(f"docker is not usable: {error}") from error
