"""In-memory stand-ins for Docker used by the container-management tests."""

from __future__ import annotations

import copy
import json
import secrets
from collections.abc import Callable, Mapping
from pathlib import Path

from backend.adapters.compose_cli import ComposeTarget
from backend.adapters.compose_overrides import FILE_NAME
from backend.adapters.container_admin import FileRead, LiveUpdate, Record
from backend.adapters.docker_stats import ContainerStats
from backend.adapters.errors import ContainerNotFoundError, ContainerRuntimeError
from backend.domain.containers import COMPOSE_PROJECT_LABEL, COMPOSE_SERVICE_LABEL
from backend.domain.resources import HostCapacity

STARTED = "2026-10-13T12:00:00.123456789Z"


def new_id() -> str:
    """A fresh 64 hex character container id."""
    return secrets.token_hex(32)


def make_record(
    *,
    project: str = "demo-dev",
    service: str = "web",
    name: str | None = None,
    container_id: str | None = None,
    running: bool = True,
    env: Mapping[str, str] | None = None,
    health: str | None = None,
    image: str = "demo/web:1",
    labels: Mapping[str, str] | None = None,
    restart_count: int = 0,
) -> dict[str, object]:
    """A ``docker inspect`` record with the fields the platform reads."""
    identity = container_id or new_id()
    all_labels = {COMPOSE_PROJECT_LABEL: project, COMPOSE_SERVICE_LABEL: service, **(labels or {})}
    state: dict[str, object] = {
        "Status": "running" if running else "exited",
        "Running": running,
        "StartedAt": STARTED,
        "ExitCode": 0,
    }
    if health:
        state["Health"] = {"Status": health}
    return {
        "Id": identity,
        "Name": f"/{name or f'{project}-{service}-1'}",
        "Image": "sha256:" + "a" * 64,
        "RestartCount": restart_count,
        "State": state,
        "Config": {
            "Image": image,
            "Labels": all_labels,
            "Env": [f"{key}={value}" for key, value in (env or {"PATH": "/usr/bin"}).items()],
        },
        "HostConfig": {"NanoCpus": 0, "Memory": 0},
        "NetworkSettings": {"Ports": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "8080"}]}},
        "Mounts": [{"Type": "volume", "Name": "data", "Destination": "/data", "RW": True}],
    }


class FakeEngine:
    """A ``ContainerEngine`` over a dict of inspect records."""

    def __init__(self) -> None:
        self.records: dict[str, dict[str, object]] = {}
        self.calls: list[tuple[str, ...]] = []
        self.log_lines: dict[str, list[str]] = {}
        self.directories: dict[str, list[str]] = {}
        self.files: dict[str, FileRead] = {}
        self.fail: dict[str, str] = {}
        self.sample_stats: list[ContainerStats] = []
        self.capacity = HostCapacity(cpus=4, memory_bytes=8 * 1024**3)

    def add(self, record: dict[str, object]) -> str:
        """Register a container; returns its id."""
        identity = str(record["Id"])
        self.records[identity] = record
        return identity

    def _check(self, method: str) -> None:
        if method in self.fail:
            raise ContainerRuntimeError(self.fail[method])

    def list_project(self, project: str) -> list[Record]:
        """All containers with the project label."""
        self._check("list_project")
        found: list[Record] = []
        for record in self.records.values():
            config = record["Config"]
            assert isinstance(config, dict)
            if config["Labels"].get(COMPOSE_PROJECT_LABEL) == project:
                found.append(copy.deepcopy(record))
        return found

    def inspect(self, container_id: str) -> Record:
        """One record or ``ContainerNotFoundError``."""
        self.calls.append(("inspect", container_id))
        if container_id not in self.records:
            raise ContainerNotFoundError("No such container")
        return copy.deepcopy(self.records[container_id])

    def _set_state(self, container_id: str, running: bool) -> None:
        state = self.records[container_id]["State"]
        assert isinstance(state, dict)
        state["Status"] = "running" if running else "exited"
        state["Running"] = running

    def start(self, container_id: str) -> None:
        """Mark running."""
        self.calls.append(("start", container_id))
        self._check("start")
        self._set_state(container_id, True)

    def stop(self, container_id: str) -> None:
        """Mark exited."""
        self.calls.append(("stop", container_id))
        self._check("stop")
        self._set_state(container_id, False)

    def restart(self, container_id: str) -> None:
        """Count a restart."""
        self.calls.append(("restart", container_id))
        self._check("restart")
        self._set_state(container_id, True)
        self.records[container_id]["RestartCount"] = (
            int(str(self.records[container_id]["RestartCount"])) + 1
        )

    def update_limits(
        self, container_id: str, cpus: float | None, memory_bytes: int | None
    ) -> None:
        """Store limits in HostConfig."""
        self.calls.append(("update_limits", container_id, str(cpus), str(memory_bytes)))
        self._check("update_limits")
        host = self.records[container_id]["HostConfig"]
        assert isinstance(host, dict)
        if cpus is not None:
            host["NanoCpus"] = int(cpus * 1_000_000_000)
        if memory_bytes is not None:
            host["Memory"] = memory_bytes

    def update_resources(self, container_id: str, update: LiveUpdate) -> None:
        """Store the values in HostConfig; refuses what the daemon refuses (a memory limit above
        the container's swap limit without a new swap limit)."""
        self.calls.append(
            (
                "update_resources",
                container_id,
                str(update.cpus),
                str(update.memory_bytes),
                str(update.memory_swap_bytes),
                str(update.memory_reservation_bytes),
                str(update.cpu_shares),
            )
        )
        self._check("update_resources")
        if container_id not in self.records:
            raise ContainerNotFoundError("No such container")
        host = self.records[container_id]["HostConfig"]
        assert isinstance(host, dict)
        swap = int(host.get("MemorySwap", 0))
        if (
            update.memory_bytes
            and update.memory_swap_bytes is None
            and 0 < swap < update.memory_bytes
        ):
            raise ContainerRuntimeError(
                "Memory limit should be smaller than already set memoryswap limit"
            )
        if update.cpus is not None:
            host["NanoCpus"] = int(update.cpus * 1_000_000_000)
        if update.memory_bytes is not None:
            host["Memory"] = update.memory_bytes
        if update.memory_swap_bytes is not None:
            host["MemorySwap"] = update.memory_swap_bytes
        if update.memory_reservation_bytes is not None:
            host["MemoryReservation"] = update.memory_reservation_bytes
        if update.cpu_shares is not None:
            host["CpuShares"] = update.cpu_shares

    def host_capacity(self) -> HostCapacity:
        """The configured host."""
        self._check("host_capacity")
        return self.capacity

    def logs(self, container_id: str, *, tail: int, since: str | None) -> list[str]:
        """Configured lines, filtered like docker (``since`` inclusive)."""
        self.calls.append(("logs", container_id, str(tail), str(since)))
        self._check("logs")
        lines = self.log_lines.get(container_id, [])
        if since is not None:
            lines = [line for line in lines if line.split(" ", 1)[0] >= since]
        return lines[-tail:]

    def list_directory(self, container_id: str, path: str, limit: int) -> list[str]:
        """Configured directory entries."""
        self.calls.append(("list_directory", container_id, path))
        self._check("list_directory")
        if path not in self.directories:
            raise ContainerNotFoundError("No such file or directory")
        return [*self.directories[path], ""]

    def read_file(self, container_id: str, path: str, limit: int) -> FileRead:
        """Configured file, cut at ``limit``."""
        self.calls.append(("read_file", container_id, path))
        self._check("read_file")
        if path not in self.files:
            raise ContainerNotFoundError("Could not find the file")
        item = self.files[path]
        return FileRead(item.kind, item.size, item.data[:limit], item.size > limit)

    def stats(self, container_ids: list[str] | None) -> list[ContainerStats]:
        """Configured samples."""
        self._check("stats")
        wanted = container_ids
        return [s for s in self.sample_stats if wanted is None or s.id in wanted]


Hook = Callable[[ComposeTarget, str, bool], None]


class FakeCompose:
    """Recreates the containers of a service in a ``FakeEngine``.

    It reads the override file among ``target.files`` (the platform's own) and applies its
    environment, like Compose would. ``crash_when`` makes the recreated service exit when a
    variable has a value; ``fail_on_call`` makes the n-th call fail.
    """

    def __init__(self, engine: FakeEngine) -> None:
        self._engine = engine
        self.calls: list[tuple[str, bool, tuple[str, ...]]] = []
        self.crash_when: tuple[str, str] | None = None
        self.fail_on_call: set[int] = set()

    def recreate(self, target: ComposeTarget, service: str, *, start: bool) -> None:
        """Apply the override file to the service's containers."""
        self.calls.append((service, start, target.files))
        if len(self.calls) in self.fail_on_call:
            raise ContainerRuntimeError("compose exploded")
        overrides = self._overrides(target)
        for old_id, record in list(self._engine.records.items()):
            config = record["Config"]
            assert isinstance(config, dict)
            labels = config["Labels"]
            if labels.get(COMPOSE_SERVICE_LABEL) != service:
                continue
            env = dict(item.split("=", 1) for item in config["Env"])
            env.update(overrides.get(service, {}))
            crashes = (
                self.crash_when is not None and env.get(self.crash_when[0]) == self.crash_when[1]
            )
            fresh = make_record(
                project=str(labels[COMPOSE_PROJECT_LABEL]),
                service=service,
                name=str(record["Name"]).lstrip("/"),
                running=start and not crashes,
                env=env,
                image=str(config["Image"]),
            )
            self._engine.records.pop(old_id)
            self._engine.records[str(fresh["Id"])] = fresh

    @staticmethod
    def _overrides(target: ComposeTarget) -> dict[str, dict[str, str]]:
        for file in target.files:
            if Path(file).name == FILE_NAME:
                services = json.loads(Path(file).read_text())["services"]
                return {
                    name: {k: v.replace("$$", "$") for k, v in entry.get("environment", {}).items()}
                    for name, entry in services.items()
                }
        return {}
