"""Host telemetry and per-container stats with small in-memory histories.

What the worker can see (documented in the API contract): ``/proc/stat``, ``/proc/meminfo`` and
``/proc/loadavg`` inside a container report the *host* kernel's numbers unless the runtime
virtualizes them (lxcfs), so CPU and RAM describe the host. The disk figures come from
``statvfs`` of the worker's data directory, i.e. the file system behind the data volume, which is
normally the host's Docker disk but is not guaranteed to be.
"""

from __future__ import annotations

import logging
import os
import threading
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from backend.adapters.container_admin import ContainerEngine
from backend.adapters.docker_inspect import parse_container
from backend.adapters.docker_stats import ContainerStats
from backend.adapters.errors import AdapterError
from backend.containers.base import Membership, TargetResolver
from backend.domain.clock import Clock, utcnow
from backend.domain.enums import EnvironmentName
from backend.domain.errors import ConflictError

logger = logging.getLogger(__name__)
KIB = 1024
CPU_FIELDS_MIN = 5  # user nice system idle iowait
MAX_AGE_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class HostSample:
    """One reading of the host as the worker sees it."""

    timestamp: datetime
    cpu_percent: float | None
    cpu_count: int
    load_1: float
    load_5: float
    load_15: float
    memory_total: int
    memory_available: int
    swap_total: int
    swap_free: int
    disk_total: int
    disk_free: int

    @property
    def memory_used(self) -> int:
        """Total minus available."""
        return max(0, self.memory_total - self.memory_available)

    @property
    def disk_used(self) -> int:
        """Total minus free (what is not available to root either counts as used)."""
        return max(0, self.disk_total - self.disk_free)


@dataclass(frozen=True, slots=True)
class ContainerSample:
    """A stats sample with its time."""

    timestamp: datetime
    stats: ContainerStats


class RingBuffer[T]:
    """A thread-safe fixed-size history."""

    def __init__(self, size: int) -> None:
        self._items: deque[T] = deque(maxlen=size)
        self._lock = threading.Lock()

    def add(self, item: T) -> None:
        """Append, dropping the oldest when full."""
        with self._lock:
            self._items.append(item)

    def items(self) -> list[T]:
        """A copy, oldest first."""
        with self._lock:
            return list(self._items)

    def last(self) -> T | None:
        """The newest item."""
        with self._lock:
            return self._items[-1] if self._items else None


def parse_cpu_times(stat_text: str) -> tuple[int, int] | None:
    """``(idle, total)`` jiffies from the aggregate ``cpu`` line of ``/proc/stat``."""
    for line in stat_text.splitlines():
        if line.startswith("cpu "):
            fields = [int(item) for item in line.split()[1:] if item.isdigit()]
            if len(fields) < CPU_FIELDS_MIN:
                return None
            idle = fields[3] + fields[4]
            return idle, sum(fields[:8])
    return None


def parse_meminfo(text: str) -> dict[str, int]:
    """``MemTotal`` and friends in bytes."""
    result: dict[str, int] = {}
    for line in text.splitlines():
        name, _sep, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            result[name] = int(parts[0]) * KIB
    return result


class HostSampler:
    """Reads ``/proc`` and ``statvfs``; CPU percentage needs two readings."""

    def __init__(
        self, disk_path: Path, proc_root: Path = Path("/proc"), clock: Clock = utcnow
    ) -> None:
        self._disk_path = disk_path
        self._proc = proc_root
        self._clock = clock
        self._previous: tuple[int, int] | None = None
        self._lock = threading.Lock()

    def sample(self) -> HostSample:
        """Take a reading; ``cpu_percent`` is ``None`` for the very first one."""
        times = parse_cpu_times((self._proc / "stat").read_text())
        memory = parse_meminfo((self._proc / "meminfo").read_text())
        load = (self._proc / "loadavg").read_text().split()
        usage = os.statvfs(_existing(self._disk_path))
        with self._lock:
            cpu = self._cpu_percent(times)
        return HostSample(
            timestamp=self._clock(),
            cpu_percent=cpu,
            cpu_count=os.cpu_count() or 1,
            load_1=float(load[0]),
            load_5=float(load[1]),
            load_15=float(load[2]),
            memory_total=memory.get("MemTotal", 0),
            memory_available=memory.get("MemAvailable", memory.get("MemFree", 0)),
            swap_total=memory.get("SwapTotal", 0),
            swap_free=memory.get("SwapFree", 0),
            disk_total=usage.f_blocks * usage.f_frsize,
            disk_free=usage.f_bavail * usage.f_frsize,
        )

    def _cpu_percent(self, times: tuple[int, int] | None) -> float | None:
        previous, self._previous = self._previous, times
        if previous is None or times is None:
            return None
        total = times[1] - previous[1]
        if total <= 0:
            return None
        busy = total - (times[0] - previous[0])
        return round(max(0.0, min(100.0, 100.0 * busy / total)), 1)


def _existing(path: Path) -> Path:
    """``path`` or its nearest existing ancestor (the data directory may not exist yet)."""
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


ProjectLister = Callable[[], Iterable[str]]


class TelemetryService:
    """Host and container telemetry with ring buffers; also the body of the collector thread."""

    def __init__(  # noqa: PLR0913, PLR0917 - collaborators of one use case
        self,
        sampler: HostSampler,
        engine: ContainerEngine,
        resolver: TargetResolver,
        membership: Membership,
        projects: ProjectLister,
        history: int,
        clock: Clock = utcnow,
    ) -> None:
        self._sampler = sampler
        self._engine = engine
        self._resolver = resolver
        self._membership = membership
        self._projects = projects
        self._size = history
        self._clock = clock
        self._host: RingBuffer[HostSample] = RingBuffer(history)
        self._containers: dict[str, RingBuffer[ContainerSample]] = {}
        self._lock = threading.Lock()

    def host(self) -> tuple[HostSample, list[HostSample]]:
        """The newest reading and the history (oldest first).

        The newest reading is the collector's, unless it is older than ``MAX_AGE_SECONDS`` (the
        collector is not running), then a fresh one is taken and recorded.
        """
        latest = self._host.last()
        if latest is None or (self._clock() - latest.timestamp).total_seconds() > MAX_AGE_SECONDS:
            latest = self._sampler.sample()
            self._host.add(latest)
        return latest, self._host.items()

    def container(
        self, slug: str, env: EnvironmentName, container_id: str
    ) -> tuple[ContainerSample, list[ContainerSample]]:
        """A fresh stats sample of one container and its history."""
        target = self._resolver.resolve(slug, env)
        info = parse_container(self._membership.require(target, container_id))
        if not info.running:
            raise ConflictError("the container is not running")
        try:
            stats = self._engine.stats([container_id])
        except AdapterError as error:
            raise ConflictError(str(error)) from error
        if not stats:
            raise ConflictError("no statistics are available for this container")
        sample = ContainerSample(self._clock(), stats[0])
        self._buffer(container_id).add(sample)
        return sample, self._buffer(container_id).items()

    def collect_once(self) -> None:
        """One background round: a host reading and stats of every running managed container."""
        self._host.add(self._sampler.sample())
        running: list[str] = []
        for project in set(self._projects()):
            try:
                records = self._engine.list_project(project)
            except AdapterError:
                logger.warning("telemetry: cannot list a project", extra={"project": project})
                continue
            running += [info.id for info in map(parse_container, records) if info.running]
        if not running:
            self._forget(set())
            return
        try:
            samples = self._engine.stats(running)
        except AdapterError:
            logger.warning("telemetry: cannot read container stats")
            return
        now = self._clock()
        for stats in samples:
            self._buffer(stats.id).add(ContainerSample(now, stats))
        self._forget(set(running))

    def _buffer(self, container_id: str) -> RingBuffer[ContainerSample]:
        with self._lock:
            return self._containers.setdefault(container_id, RingBuffer(self._size))

    def _forget(self, alive: set[str]) -> None:
        with self._lock:
            for container_id in set(self._containers) - alive:
                del self._containers[container_id]


class TelemetryCollector(threading.Thread):
    """Calls ``collect_once`` periodically until stopped."""

    def __init__(self, service: TelemetryService, interval: float) -> None:
        super().__init__(name="telemetry", daemon=True)
        self._service = service
        self._interval = interval
        self._stop_event = threading.Event()

    def run(self) -> None:
        """Collect, wait, repeat; a failed round is logged and does not end the thread."""
        while True:
            try:
                self._service.collect_once()
            except Exception:
                logger.exception("telemetry round failed")
            if self._stop_event.wait(self._interval):
                return

    def stop(self) -> None:
        """Ask the thread to end and wait briefly."""
        self._stop_event.set()
        if self.is_alive():
            self.join(5)
