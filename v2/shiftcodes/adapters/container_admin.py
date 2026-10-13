"""Management operations on existing containers through the docker CLI.

Every method takes the full container id and passes it as one argv element. Nothing is run
through a shell and no user text is ever interpolated into a command string.
"""

from __future__ import annotations

import json
import tarfile
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from shiftcodes.adapters.docker_client import DockerClient
from shiftcodes.adapters.docker_stats import ContainerStats, parse_stats
from shiftcodes.adapters.errors import ContainerNotFoundError, ContainerRuntimeError
from shiftcodes.domain.containers import COMPOSE_PROJECT_LABEL

Record = Mapping[str, object]
STOP_TIMEOUT_SECONDS = 10
LIFECYCLE_TIMEOUT = 60.0
STATS_TIMEOUT = 60.0
TAR_BLOCK = 512
MAX_LOG_BYTES = 900 * 1024
MAX_ERROR_CHARS = 300
_NOT_FOUND_MARKERS = ("no such", "could not find", "not found")
_NO_EXEC_MARKERS = ("executable file not found", "no such file or directory: unknown")
_EXTENDED_TYPES = {
    tarfile.XHDTYPE,
    tarfile.XGLTYPE,
    tarfile.GNUTYPE_LONGNAME,
    tarfile.GNUTYPE_LONGLINK,
}
_MAX_TAR_PREFIX_HEADERS = 4


@dataclass(frozen=True, slots=True)
class FileRead:
    """The first member of the tar stream ``docker cp`` produced."""

    kind: str  # file | directory | symlink | other
    size: int
    data: bytes
    truncated: bool


class ContainerEngine(Protocol):
    """What container management needs from Docker; the CLI implements it, tests fake it."""

    def list_project(self, project: str) -> list[Record]:
        """Inspect records of every container (any state) of a compose project."""

    def inspect(self, container_id: str) -> Record:
        """The inspect record of one container; raises ``ContainerNotFoundError``."""

    def start(self, container_id: str) -> None:
        """Start a container."""

    def stop(self, container_id: str) -> None:
        """Stop a container."""

    def restart(self, container_id: str) -> None:
        """Restart a container."""

    def update_limits(
        self, container_id: str, cpus: float | None, memory_bytes: int | None
    ) -> None:
        """Apply CPU and/or memory limits to a live container (``None`` leaves one alone)."""

    def logs(self, container_id: str, *, tail: int, since: str | None) -> list[str]:
        """Raw ``<timestamp> <text>`` lines (stdout and stderr merged), oldest first."""

    def list_directory(self, container_id: str, path: str, limit: int) -> list[str]:
        """Entry names of a directory, directories with a trailing ``/``."""

    def read_file(self, container_id: str, path: str, limit: int) -> FileRead:
        """Up to ``limit`` bytes of one file."""

    def stats(self, container_ids: list[str] | None) -> list[ContainerStats]:
        """One stats sample for the given containers (all running ones when ``None``)."""


def _first_line(text: str) -> str:
    line = next((item.strip() for item in text.splitlines() if item.strip()), "")
    return line[:MAX_ERROR_CHARS]


def _failure(prefix: str, stderr: str) -> ContainerRuntimeError:
    message = _first_line(stderr)
    detail = f"{prefix}: {message}" if message else prefix
    if any(marker in message.lower() for marker in _NOT_FOUND_MARKERS):
        return ContainerNotFoundError(detail)
    return ContainerRuntimeError(detail)


class DockerContainerEngine:
    """``ContainerEngine`` on top of ``DockerClient``."""

    def __init__(self, client: DockerClient) -> None:
        self._client = client

    def list_project(self, project: str) -> list[Record]:
        """Inspect every container carrying the compose project label."""
        listing = self._client.run(
            [
                "container",
                "ls",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                f"label={COMPOSE_PROJECT_LABEL}={project}",
            ]
        )
        if listing.returncode != 0:
            raise _failure("cannot list containers", listing.stderr)
        ids = listing.stdout.split()
        return self._inspect_many(ids) if ids else []

    def inspect(self, container_id: str) -> Record:
        """Inspect one container by its full id."""
        records = self._inspect_many([container_id])
        if not records:
            raise ContainerNotFoundError("container not found")
        return records[0]

    def _inspect_many(self, ids: list[str]) -> list[Record]:
        result = self._client.run(["inspect", "--type", "container", *ids])
        if result.returncode != 0:
            raise _failure("cannot inspect container", result.stderr)
        try:
            parsed = json.loads(result.stdout)
        except ValueError as error:
            raise ContainerRuntimeError("docker returned unreadable inspect output") from error
        if not isinstance(parsed, list):
            return []
        return [item for item in parsed if isinstance(item, dict)]

    def start(self, container_id: str) -> None:
        """``docker start``."""
        self._lifecycle(["start", container_id], "cannot start")

    def stop(self, container_id: str) -> None:
        """``docker stop`` with a bounded grace period."""
        self._lifecycle(["stop", "--time", str(STOP_TIMEOUT_SECONDS), container_id], "cannot stop")

    def restart(self, container_id: str) -> None:
        """``docker restart`` with a bounded grace period."""
        args = ["restart", "--time", str(STOP_TIMEOUT_SECONDS), container_id]
        self._lifecycle(args, "cannot restart")

    def _lifecycle(self, args: list[str], prefix: str) -> None:
        result = self._client.run(["container", *args], timeout=LIFECYCLE_TIMEOUT)
        if result.returncode != 0:
            raise _failure(prefix, result.stderr)

    def update_limits(
        self, container_id: str, cpus: float | None, memory_bytes: int | None
    ) -> None:
        """``docker update``; swap is set equal to memory so the limit is real."""
        args = ["update"]
        if cpus is not None:
            args += ["--cpus", f"{cpus:g}"]
        if memory_bytes is not None:
            args += ["--memory", str(memory_bytes), "--memory-swap", str(memory_bytes)]
        result = self._client.run(["container", *args, container_id])
        if result.returncode != 0:
            raise _failure("cannot update limits", result.stderr)

    def logs(self, container_id: str, *, tail: int, since: str | None) -> list[str]:
        """Timestamped log lines, stdout and stderr merged."""
        args = ["container", "logs", "--timestamps", "--tail", str(tail)]
        if since is not None:
            args += ["--since", since]
        output = self._client.run_bounded(
            [*args, container_id], limit=MAX_LOG_BYTES, merge_stderr=True
        )
        text = output.data.decode("utf-8", errors="replace")
        if output.returncode not in (0, None):
            raise _failure("cannot read logs", text)
        return text.splitlines()

    def list_directory(self, container_id: str, path: str, limit: int) -> list[str]:
        """``ls -1Ap`` run inside the container (needs ``ls``; no shell is involved)."""
        output = self._client.run_bounded(
            ["container", "exec", container_id, "ls", "-1Ap", "--", path], limit=limit
        )
        if output.returncode not in (0, None):
            lowered = output.stderr.lower()
            if any(marker in lowered for marker in _NO_EXEC_MARKERS):
                raise ContainerRuntimeError("the container has no 'ls' command")
            raise _failure("cannot list directory", output.stderr)
        return output.data.decode("utf-8", errors="replace").split("\n")

    def read_file(self, container_id: str, path: str, limit: int) -> FileRead:
        """The first tar member of ``docker cp <id>:<path> -``, cut at ``limit`` bytes."""
        budget = limit + TAR_BLOCK * (_MAX_TAR_PREFIX_HEADERS + 2) + 4096
        output = self._client.run_bounded(
            ["container", "cp", f"{container_id}:{path}", "-"], limit=budget
        )
        if output.returncode not in (0, None):
            raise _failure("cannot read file", output.stderr)
        return parse_tar_member(output.data, limit)

    def stats(self, container_ids: list[str] | None) -> list[ContainerStats]:
        """``docker stats --no-stream``."""
        args = ["container", "stats", "--no-stream", "--no-trunc", "--format", "{{json .}}"]
        result = self._client.run([*args, *(container_ids or [])], timeout=STATS_TIMEOUT)
        if result.returncode != 0:
            raise _failure("cannot read stats", result.stderr)
        return parse_stats(result.stdout)


def parse_tar_member(data: bytes, limit: int) -> FileRead:
    """Read the first real member of a (possibly cut) tar stream."""
    offset = 0
    for _ in range(_MAX_TAR_PREFIX_HEADERS + 1):
        header = data[offset : offset + TAR_BLOCK]
        if len(header) < TAR_BLOCK:
            raise ContainerRuntimeError("docker returned an unreadable archive")
        try:
            info = tarfile.TarInfo.frombuf(header, "utf-8", "surrogateescape")
        except tarfile.TarError as error:
            raise ContainerRuntimeError("docker returned an unreadable archive") from error
        offset += TAR_BLOCK
        if info.type not in _EXTENDED_TYPES:
            break
        offset += -(-info.size // TAR_BLOCK) * TAR_BLOCK
    else:
        raise ContainerRuntimeError("docker returned an unreadable archive")
    if info.isdir():
        return FileRead("directory", 0, b"", False)
    if info.issym() or info.islnk():
        return FileRead("symlink", 0, b"", False)
    if not info.isreg():
        return FileRead("other", 0, b"", False)
    wanted = min(info.size, limit)
    content = data[offset : offset + wanted]
    if len(content) < wanted:
        raise ContainerRuntimeError("docker returned a truncated archive")
    return FileRead("file", info.size, content, info.size > limit)
