"""A small docker command line client for management operations (never a shell).

Every call is an explicit argument list. Output is decoded leniently; commands that can
produce unbounded output are read through ``run_bounded`` which stops the process at a limit.
"""

from __future__ import annotations

import contextlib
import subprocess
import tempfile
import threading
from collections.abc import Mapping
from dataclasses import dataclass

from backend.adapters.docker_cli import docker_client_env
from backend.adapters.errors import ContainerRuntimeError

DEFAULT_TIMEOUT = 30.0
KILL_GRACE = 2.0


@dataclass(frozen=True, slots=True)
class CommandOutput:
    """Result of a finished command."""

    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class BoundedOutput:
    """Output of a command stopped at a size limit."""

    data: bytes
    truncated: bool
    returncode: int | None
    stderr: str


class DockerClient:
    """Runs ``docker`` with a minimal environment (``DOCKER_HOST`` and a few client variables)."""

    def __init__(self, binary: str = "docker", docker_host: str | None = None) -> None:
        self._binary = binary
        self._env = docker_client_env(docker_host)

    @property
    def binary(self) -> str:
        """The docker executable."""
        return self._binary

    @property
    def env(self) -> Mapping[str, str]:
        """The environment passed to every docker process."""
        return self._env

    def run(self, args: list[str], *, timeout: float = DEFAULT_TIMEOUT) -> CommandOutput:
        """Run ``docker <args>`` to completion; transport problems raise."""
        try:
            done = subprocess.run(  # noqa: S603 - explicit argv list, never a shell
                [self._binary, *args],
                capture_output=True,
                timeout=timeout,
                env=dict(self._env),
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise ContainerRuntimeError("docker did not answer in time") from error
        except (OSError, subprocess.SubprocessError) as error:
            raise ContainerRuntimeError(f"docker is not usable: {error}") from error
        return CommandOutput(
            done.returncode,
            done.stdout.decode("utf-8", errors="replace"),
            done.stderr.decode("utf-8", errors="replace"),
        )

    def popen(self, args: list[str], **kwargs: object) -> subprocess.Popen[bytes]:
        """Start ``docker <args>``; the caller owns the process. ``kwargs`` go to Popen."""
        try:
            return subprocess.Popen(  # type: ignore[call-overload,no-any-return]  # noqa: S603
                [self._binary, *args], env=dict(self._env), **kwargs
            )
        except OSError as error:
            raise ContainerRuntimeError(f"docker is not usable: {error}") from error

    def run_bounded(
        self,
        args: list[str],
        *,
        limit: int,
        timeout: float = DEFAULT_TIMEOUT,
        merge_stderr: bool = False,
    ) -> BoundedOutput:
        """Run a command and read at most ``limit`` bytes of stdout, then stop it.

        With ``merge_stderr`` the two streams are interleaved in the returned data.
        """
        with tempfile.TemporaryFile() as errors:
            process = self.popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT if merge_stderr else errors,
            )
            timer = threading.Timer(timeout, process.kill)
            timer.start()
            try:
                stdout = process.stdout
                data = stdout.read(limit + 1) if stdout is not None else b""
                truncated = len(data) > limit
                if truncated:
                    process.kill()
                returncode: int | None = process.wait(KILL_GRACE + timeout)
                if truncated:
                    data, returncode = data[:limit], None
            finally:
                timer.cancel()
                process.kill()
                if process.stdout is not None:
                    with contextlib.suppress(OSError):
                        process.stdout.close()
                process.wait()
            errors.seek(0)
            stderr = errors.read(4096).decode("utf-8", errors="replace")
        return BoundedOutput(data, truncated, returncode, stderr)
