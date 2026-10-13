"""The JSON shapes of container management, shared by the worker RPC and the HTTP API.

The worker dumps these models, the API client validates them again and the routers return them
unchanged, so the contract in ``docs/api-contract.md`` has exactly one definition.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict

from backend.adapters.docker_stats import ContainerStats
from backend.containers.environment import EnvUpdateResult
from backend.containers.files import DirectoryListing, FileContent
from backend.containers.lifecycle import ActionPlan, ActionResult
from backend.containers.logs import LogCursor, LogPage
from backend.containers.telemetry import ContainerSample, HostSample
from backend.domain.containers import ContainerInfo, EnvVarInfo, MountInfo, PortBinding


class View(BaseModel):
    """Base of every wire model."""

    model_config = ConfigDict(frozen=True)


class PortView(View):
    """A port and its host binding."""

    container_port: str
    host_ip: str | None
    host_port: str | None

    @classmethod
    def of(cls, port: PortBinding) -> PortView:
        """From the domain value."""
        return cls(
            container_port=port.container_port, host_ip=port.host_ip, host_port=port.host_port
        )


class MountView(View):
    """A mount."""

    type: str
    source: str
    destination: str
    read_only: bool

    @classmethod
    def of(cls, mount: MountInfo) -> MountView:
        """From the domain value."""
        return cls(
            type=mount.type,
            source=mount.source,
            destination=mount.destination,
            read_only=mount.read_only,
        )


class ContainerView(View):
    """One inventory entry."""

    id: str
    name: str
    service: str | None
    image: str
    image_id: str
    revision: str | None
    commit: str | None
    state: str
    health: str | None
    restart_count: int
    started_at: datetime | None
    exit_code: int | None
    cpus: float | None
    memory_bytes: int | None
    ports: tuple[PortView, ...]
    mounts: tuple[MountView, ...]

    @classmethod
    def of(cls, info: ContainerInfo) -> ContainerView:
        """From the domain value."""
        return cls(
            id=info.id,
            name=info.name,
            service=info.service,
            image=info.image,
            image_id=info.image_id,
            revision=info.revision,
            commit=info.commit,
            state=info.state,
            health=info.health,
            restart_count=info.restart_count,
            started_at=info.started_at,
            exit_code=info.exit_code,
            cpus=info.cpus,
            memory_bytes=info.memory_bytes,
            ports=tuple(PortView.of(port) for port in info.ports),
            mounts=tuple(MountView.of(mount) for mount in info.mounts),
        )


class ActionPlanView(View):
    """Step one of start/stop/restart."""

    action: str
    container: ContainerView
    effects: tuple[str, ...]
    confirmation: str
    expires_at: datetime

    @classmethod
    def of(cls, plan: ActionPlan) -> ActionPlanView:
        """From the domain value."""
        return cls(
            action=plan.action.value,
            container=ContainerView.of(plan.container),
            effects=plan.effects,
            confirmation=plan.confirmation,
            expires_at=plan.expires_at,
        )


class ActionResultView(View):
    """Step two of start/stop/restart."""

    action: str
    previous_state: str
    container: ContainerView

    @classmethod
    def of(cls, result: ActionResult) -> ActionResultView:
        """From the domain value."""
        return cls(
            action=result.action.value,
            previous_state=result.previous_state,
            container=ContainerView.of(result.container),
        )


class LogLineView(View):
    """One masked log line."""

    timestamp: str
    text: str


class LogCursorView(View):
    """Where a follower continues."""

    since: str
    skip: int

    @classmethod
    def of(cls, cursor: LogCursor) -> LogCursorView:
        """From the domain value."""
        return cls(since=cursor.since, skip=cursor.skip)


class LogPageView(View):
    """A batch of lines."""

    lines: tuple[LogLineView, ...]
    cursor: LogCursorView | None
    dropped: int

    @classmethod
    def of(cls, page: LogPage) -> LogPageView:
        """From the domain value."""
        return cls(
            lines=tuple(
                LogLineView(timestamp=line.timestamp, text=line.text) for line in page.lines
            ),
            cursor=LogCursorView.of(page.cursor) if page.cursor else None,
            dropped=page.dropped,
        )


class EnvVarView(View):
    """A variable name; values are never listed."""

    name: str
    sensitive: bool
    overridden: bool
    masked_value: str = "********"

    @classmethod
    def of(cls, item: EnvVarInfo) -> EnvVarView:
        """From the domain value."""
        return cls(name=item.name, sensitive=item.sensitive, overridden=item.overridden)


class EnvRevealView(View):
    """The plaintext of one variable (audited)."""

    name: str
    value: str


class EnvUpdateView(View):
    """The service's containers after an edit."""

    service: str
    restarted: bool
    containers: tuple[ContainerView, ...]

    @classmethod
    def of(cls, result: EnvUpdateResult) -> EnvUpdateView:
        """From the domain value."""
        return cls(
            service=result.service,
            restarted=result.restarted,
            containers=tuple(ContainerView.of(item) for item in result.containers),
        )


class DirectoryEntryView(View):
    """A directory entry."""

    name: str
    kind: str


class DirectoryView(View):
    """A directory listing."""

    path: str
    entries: tuple[DirectoryEntryView, ...]
    truncated: bool

    @classmethod
    def of(cls, listing: DirectoryListing) -> DirectoryView:
        """From the domain value."""
        return cls(
            path=listing.path,
            entries=tuple(DirectoryEntryView(name=e.name, kind=e.kind) for e in listing.entries),
            truncated=listing.truncated,
        )


class FileView(View):
    """A file's content (text) or the information that it is binary."""

    path: str
    size: int
    truncated: bool
    binary: bool
    text: str | None

    @classmethod
    def of(cls, content: FileContent) -> FileView:
        """From the domain value."""
        return cls(
            path=content.path,
            size=content.size,
            truncated=content.truncated,
            binary=content.binary,
            text=content.text,
        )


class HostSampleView(View):
    """One host reading."""

    timestamp: datetime
    cpu_percent: float | None
    cpu_count: int
    load_1: float
    load_5: float
    load_15: float
    memory_total: int
    memory_used: int
    memory_available: int
    swap_total: int
    swap_free: int
    disk_total: int
    disk_used: int
    disk_free: int

    @classmethod
    def of(cls, sample: HostSample) -> HostSampleView:
        """From the domain value."""
        return cls(
            timestamp=sample.timestamp,
            cpu_percent=sample.cpu_percent,
            cpu_count=sample.cpu_count,
            load_1=sample.load_1,
            load_5=sample.load_5,
            load_15=sample.load_15,
            memory_total=sample.memory_total,
            memory_used=sample.memory_used,
            memory_available=sample.memory_available,
            swap_total=sample.swap_total,
            swap_free=sample.swap_free,
            disk_total=sample.disk_total,
            disk_used=sample.disk_used,
            disk_free=sample.disk_free,
        )


class HostTelemetryView(View):
    """Latest host reading and history."""

    latest: HostSampleView
    history: tuple[HostSampleView, ...]


class ContainerSampleView(View):
    """One stats sample of a container."""

    timestamp: datetime
    cpu_percent: float
    memory_used: int
    memory_limit: int
    memory_percent: float
    net_rx: int
    net_tx: int
    block_read: int
    block_write: int
    pids: int

    @classmethod
    def of(cls, sample: ContainerSample) -> ContainerSampleView:
        """From the domain value."""
        stats: ContainerStats = sample.stats
        return cls(
            timestamp=sample.timestamp,
            cpu_percent=stats.cpu_percent,
            memory_used=stats.memory_used,
            memory_limit=stats.memory_limit,
            memory_percent=stats.memory_percent,
            net_rx=stats.net_rx,
            net_tx=stats.net_tx,
            block_read=stats.block_read,
            block_write=stats.block_write,
            pids=stats.pids,
        )


class ContainerStatsView(View):
    """Latest stats sample of a container and its history."""

    latest: ContainerSampleView
    history: tuple[ContainerSampleView, ...]
