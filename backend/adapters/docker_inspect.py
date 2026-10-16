"""Turning ``docker inspect`` JSON into domain values. Pure functions, no process is run."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from backend.domain.containers import (
    COMMIT_LABEL,
    COMPOSE_PROJECT_LABEL,
    COMPOSE_SERVICE_LABEL,
    OCI_REVISION_LABEL,
    ContainerInfo,
    MountInfo,
    PortBinding,
)

Record = Mapping[str, object]
NANO = 1_000_000_000
ZERO_TIME_PREFIX = "0001-01-01"


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _number(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def parse_docker_time(value: object) -> datetime | None:
    """Parse Docker's UTC RFC 3339 nanosecond timestamps; the zero time means "never"."""
    text = _text(value)
    if text is None or text.startswith(ZERO_TIME_PREFIX):
        return None
    head, _dot, tail = text.rstrip("Z").partition(".")
    micro = "".join(char for char in tail if char.isdigit())[:6].ljust(6, "0")
    try:
        parsed = datetime.fromisoformat(head[:19])
    except ValueError:
        return None
    return parsed.replace(microsecond=int(micro), tzinfo=UTC)


def labels_of(record: Record) -> Mapping[str, object]:
    """The container's labels (image labels are merged in by Docker already)."""
    return _mapping(_mapping(record.get("Config")).get("Labels"))


def compose_project_of(record: Record) -> str | None:
    """The compose project label."""
    return _text(labels_of(record).get(COMPOSE_PROJECT_LABEL))


def service_of(record: Record) -> str | None:
    """The compose service label."""
    return _text(labels_of(record).get(COMPOSE_SERVICE_LABEL))


def depends_on_of(record: Record) -> tuple[str, ...]:
    """Parse Compose's ``service:condition:restart`` dependency label."""
    raw = labels_of(record).get("com.docker.compose.depends_on")
    if not isinstance(raw, str):
        return ()
    return tuple(sorted({item.split(":", 1)[0] for item in raw.split(",") if item}))


def networks_of(record: Record) -> tuple[str, ...]:
    """Network names attached to a container, in stable order."""
    networks = _mapping(_mapping(record.get("NetworkSettings")).get("Networks"))
    return tuple(sorted(str(name) for name in networks))


def parse_ports(record: Record) -> tuple[PortBinding, ...]:
    """Published and exposed ports in a stable order."""
    ports = _mapping(_mapping(record.get("NetworkSettings")).get("Ports"))
    result: list[PortBinding] = []
    for container_port in sorted(ports):
        bindings = ports[container_port]
        if not isinstance(bindings, list) or not bindings:
            result.append(PortBinding(container_port))
            continue
        for binding in bindings:
            item = _mapping(binding)
            result.append(
                PortBinding(container_port, _text(item.get("HostIp")), _text(item.get("HostPort")))
            )
    return tuple(result)


def parse_mounts(record: Record) -> tuple[MountInfo, ...]:
    """Mounts: type, host source (or volume name), destination and mode."""
    mounts = record.get("Mounts")
    if not isinstance(mounts, list):
        return ()
    result = []
    for raw in mounts:
        item = _mapping(raw)
        source = _text(item.get("Source")) or _text(item.get("Name")) or ""
        result.append(
            MountInfo(
                type=_text(item.get("Type")) or "unknown",
                source=source,
                destination=_text(item.get("Destination")) or "",
                read_only=item.get("RW") is False,
            )
        )
    return tuple(sorted(result, key=lambda mount: mount.destination))


def parse_container(record: Record) -> ContainerInfo:
    """The inventory entry of one inspected container."""
    state = _mapping(record.get("State"))
    config = _mapping(record.get("Config"))
    host_config = _mapping(record.get("HostConfig"))
    labels = labels_of(record)
    health = _text(_mapping(state.get("Health")).get("Status"))
    nano_cpus = _number(host_config.get("NanoCpus"))
    memory = _number(host_config.get("Memory"))
    return ContainerInfo(
        id=str(record.get("Id", "")),
        name=str(record.get("Name", "")).lstrip("/"),
        service=service_of(record),
        image=_text(config.get("Image")) or "",
        image_id=_text(record.get("Image")) or "",
        revision=_text(labels.get(OCI_REVISION_LABEL)),
        commit=_text(labels.get(COMMIT_LABEL)),
        state=_text(state.get("Status")) or "unknown",
        health=health,
        restart_count=_number(record.get("RestartCount")),
        started_at=parse_docker_time(state.get("StartedAt")),
        ports=parse_ports(record),
        mounts=parse_mounts(record),
        cpus=nano_cpus / NANO if nano_cpus > 0 else None,
        memory_bytes=memory if memory > 0 else None,
        exit_code=_number(state.get("ExitCode")) if state.get("Status") == "exited" else None,
    )


def parse_env(record: Record) -> dict[str, str]:
    """The environment of the container as ``NAME -> value`` (later entries win)."""
    raw = _mapping(record.get("Config")).get("Env")
    result: dict[str, str] = {}
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, str):
                name, sep, value = entry.partition("=")
                if sep:
                    result[name] = value
    return result
