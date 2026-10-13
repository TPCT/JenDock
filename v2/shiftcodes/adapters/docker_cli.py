"""``ContainerRuntime`` implemented with the docker command line client.

Every call is an explicit argument list (never a shell). Environments, including secrets,
reach containers through a temporary ``--env-file`` that is created 0600 in a worker-private
directory and deleted as soon as the daemon has created the container.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import threading
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path

from shiftcodes.adapters.container_runtime import (
    BuildSpec,
    CancelSignal,
    ExecResult,
    LineSink,
    RunSpec,
)
from shiftcodes.adapters.docker_args import build_argv, env_file_text, run_argv
from shiftcodes.adapters.docker_process import stream_process
from shiftcodes.adapters.errors import ContainerRuntimeError

logger = logging.getLogger(__name__)
QUERY_TIMEOUT = 30.0
INSPECT_INTERVAL = 0.15
PRIVATE_DIR_MODE = 0o700
ENV_FILE_MODE = 0o600
_BASE_ENV_KEYS = ("PATH", "HOME", "LANG", "DOCKER_CONFIG", "DOCKER_CERT_PATH", "DOCKER_TLS_VERIFY")


class _EnvFile:
    """A 0600 file removed on ``release`` (idempotent, thread-safe)."""

    def __init__(self, directory: Path, text: str) -> None:
        directory.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
        self.path = directory / f"env-{uuid.uuid4().hex}"
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, ENV_FILE_MODE)
        try:
            os.fchmod(fd, ENV_FILE_MODE)
            os.write(fd, text.encode())
        finally:
            os.close(fd)
        self._lock = threading.Lock()

    def release(self) -> None:
        """Delete the file."""
        with self._lock:
            self.path.unlink(missing_ok=True)


class DockerCliRuntime:
    """Docker through its CLI.

    ``extra_labels`` are added to every container and image this runtime creates (tests use
    them to find and remove what they made).
    """

    def __init__(
        self,
        *,
        binary: str = "docker",
        docker_host: str | None = None,
        private_dir: Path,
        extra_labels: Mapping[str, str] | None = None,
    ) -> None:
        self._binary = binary
        self._private_dir = private_dir
        self._labels = dict(extra_labels or {})
        self._env = self._client_env(docker_host)

    @staticmethod
    def _client_env(docker_host: str | None) -> dict[str, str]:
        env = {key: os.environ[key] for key in _BASE_ENV_KEYS if key in os.environ}
        if docker_host:
            env["DOCKER_HOST"] = docker_host
        elif "DOCKER_HOST" in os.environ:
            env["DOCKER_HOST"] = os.environ["DOCKER_HOST"]
        return env

    # --- queries ------------------------------------------------------------------

    def query(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        """Run a short docker command and capture its output."""
        try:
            return subprocess.run(  # noqa: S603 - explicit argv list, never a shell
                [self._binary, *args],
                capture_output=True,
                text=True,
                timeout=QUERY_TIMEOUT,
                env=self._env,
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ContainerRuntimeError(f"docker is not usable: {error}") from error

    def image_exists(self, image: str) -> bool:
        """Whether the image is present locally."""
        return self.query(["image", "inspect", "--format", "{{.Id}}", image]).returncode == 0

    def network_exists(self, name: str) -> bool:
        """Whether a Docker network called ``name`` exists."""
        return self.query(["network", "inspect", "--format", "{{.Id}}", name]).returncode == 0

    def kill_by_label(self, label: str) -> int:
        """Kill running containers carrying ``label``; return how many were targeted."""
        listing = self.query(["ps", "--quiet", "--no-trunc", "--filter", f"label={label}"])
        ids = listing.stdout.split()
        if listing.returncode != 0 or not ids:
            return 0
        self.query(["kill", *ids])
        return len(ids)

    def remove_image(self, image: str) -> None:
        """Remove an image tag; a missing image is not an error."""
        result = self.query(["rmi", image])
        if result.returncode != 0 and "No such image" not in result.stderr:
            raise ContainerRuntimeError(f"cannot remove image {image}: {result.stderr.strip()}")

    # --- streamed operations ------------------------------------------------------

    def pull_image(
        self,
        image: str,
        sink: LineSink,
        *,
        timeout: float | None = None,
        cancel: CancelSignal | None = None,
    ) -> ExecResult:
        """Pull an image quietly (the output is the image id)."""
        argv = [self._binary, "pull", "--quiet", image]
        return self._stream(argv, self._env, sink, timeout, cancel)

    def build_image(
        self,
        spec: BuildSpec,
        sink: LineSink,
        *,
        timeout: float | None = None,
        cancel: CancelSignal | None = None,
    ) -> ExecResult:
        """Build an image. Build argument values travel in the client's environment."""
        argv = build_argv(self._binary, spec, self._labels)
        env = {**spec.build_args, **self._env}  # reserved names are rejected by build_argv
        return self._stream(argv, env, sink, timeout, cancel)

    def run_container(
        self,
        spec: RunSpec,
        sink: LineSink,
        *,
        timeout: float | None = None,
        cancel: CancelSignal | None = None,
    ) -> ExecResult:
        """Run a container; the env file is deleted once the container exists."""
        env_file = _EnvFile(self._private_dir, env_file_text(spec.env)) if spec.env else None
        try:
            argv = run_argv(
                self._binary, spec, self._labels, str(env_file.path) if env_file else None
            )
            watcher = _CreationWatcher(self, spec.name, env_file)
            return self._stream(
                argv,
                self._env,
                sink,
                timeout,
                cancel,
                on_abort=lambda: self._kill_named(spec.name),
                watcher=watcher,
            )
        finally:
            if env_file is not None:
                env_file.release()

    def _kill_named(self, name: str) -> None:
        self.query(["kill", name])

    def _stream(  # noqa: PLR0913 - mirrors stream_process
        self,
        argv: list[str],
        env: Mapping[str, str],
        sink: LineSink,
        timeout: float | None,
        cancel: CancelSignal | None,
        *,
        on_abort: Callable[[], None] | None = None,
        watcher: _CreationWatcher | None = None,
    ) -> ExecResult:
        try:
            return stream_process(
                argv,
                env,
                sink,
                timeout=timeout,
                cancel=cancel,
                on_abort=on_abort,
                on_started=watcher.start if watcher else None,
                on_first_line=watcher.release if watcher else None,
            )
        except OSError as error:
            raise ContainerRuntimeError(f"docker is not usable: {error}") from error
        finally:
            if watcher is not None:
                watcher.stop()


class _CreationWatcher:
    """Deletes the env file once ``docker inspect`` finds the named container.

    The client reads the env file before it asks the daemon to create the container, so
    the file is no longer needed from then on. Output of the container or its exit also
    release it, whichever happens first.
    """

    def __init__(self, runtime: DockerCliRuntime, name: str, env_file: _EnvFile | None) -> None:
        self._runtime = runtime
        self._name = name
        self._env_file = env_file
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._poll, name="env-file-reaper", daemon=True)

    def start(self, _process: object = None) -> None:
        """Begin polling."""
        if self._env_file is not None:
            self._thread.start()

    def release(self) -> None:
        """Delete the file now."""
        if self._env_file is not None:
            self._env_file.release()

    def stop(self) -> None:
        """Stop polling and delete the file."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(QUERY_TIMEOUT)
        self.release()

    def _poll(self) -> None:
        while not self._stop.wait(INSPECT_INTERVAL):
            with contextlib.suppress(ContainerRuntimeError):
                found = self._runtime.query(["inspect", "--format", "{{.Id}}", self._name])
                if found.returncode == 0:
                    self.release()
                    return
