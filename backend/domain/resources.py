"""Resource limits of a compose service: Docker-style parsing (``512m``, ``2g``, ``1.5``), the
value objects and the validation against what the host has."""

from __future__ import annotations

import re
from dataclasses import dataclass

from backend.domain.errors import ValidationError

_MEMORY = re.compile(r"([0-9]+)([bkmgBKMG]?)")
_CPUS = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_UNITS = {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


def parse_memory(text: str) -> int:
    """Return the number of bytes of a Docker memory size; a bare number means bytes."""
    match = _MEMORY.fullmatch(text)
    if match is None:
        raise ValidationError(f"invalid memory size: {text!r} (use for example 512m or 2g)")
    size = int(match.group(1)) * _UNITS[match.group(2).lower()]
    if size <= 0:
        raise ValidationError(f"memory size must be greater than zero: {text!r}")
    return size


def parse_cpus(text: str) -> float:
    """Return the CPU count of a Docker ``--cpus`` value."""
    if _CPUS.fullmatch(text) is None:
        raise ValidationError(f"invalid cpu count: {text!r}")
    cpus = float(text)
    if cpus <= 0:
        raise ValidationError(f"cpu count must be greater than zero: {text!r}")
    return cpus


MIB = 1024**2
GIB = 1024**3
MIN_MEMORY_BYTES = 6 * MIB  # Docker refuses a memory limit below 6 MiB
MIN_CPUS = 0.01  # the finest CPU quota Docker accepts
MIN_CPU_SHARES = 2
MAX_CPU_SHARES = 262_144
_SERVICE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}")


@dataclass(frozen=True, slots=True)
class HostCapacity:
    """What the Docker host offers: the ceiling of every limit."""

    cpus: int
    memory_bytes: int


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    """The resource limits of one compose service; ``None`` means "not limited by Jendock".

    ``cpus`` is a CPU quota in cores (``0.5`` is half a core), ``memory_bytes`` a hard limit,
    ``memory_reservation_bytes`` a soft limit Docker enforces only under memory pressure and
    ``cpu_shares`` a relative CPU weight (Docker's default is 1024) that matters only when the
    host is busy. Swap is never given on top of the memory limit (``memory_swap_bytes``).
    """

    cpus: float | None = None
    memory_bytes: int | None = None
    memory_reservation_bytes: int | None = None
    cpu_shares: int | None = None

    @property
    def is_empty(self) -> bool:
        """Whether no limit is set."""
        return (
            self.cpus is None
            and self.memory_bytes is None
            and self.memory_reservation_bytes is None
            and self.cpu_shares is None
        )

    @property
    def memory_swap_bytes(self) -> int | None:
        """Total of memory and swap: equal to the memory limit, so the limit is real."""
        return self.memory_bytes


def format_size(size: int) -> str:
    """A byte count for messages: ``512 MiB``, ``1.5 GiB``."""
    if size >= GIB:
        return f"{size / GIB:g} GiB"
    return f"{size / MIB:g} MiB"


def validate_service_name(name: str) -> str:
    """A compose service name (letters, digits, ``.``, ``_``, ``-``)."""
    if _SERVICE_NAME.fullmatch(name) is None:
        raise ValidationError(f"invalid service name: {name!r}")
    return name


def validate_limits(limits: ResourceLimits, host: HostCapacity) -> ResourceLimits:
    """Check ``limits`` against what Docker accepts and what the host has.

    Raises ``ValidationError`` naming the first offending value in plain words.
    """
    if limits.is_empty:
        raise ValidationError("set at least one limit, or remove the limit instead")
    _check_cpus(limits.cpus, host)
    _check_memory("memory", limits.memory_bytes, host)
    _check_memory("memory reservation", limits.memory_reservation_bytes, host)
    reservation, memory = limits.memory_reservation_bytes, limits.memory_bytes
    if reservation is not None and memory is not None and reservation > memory:
        raise ValidationError("the memory reservation cannot be larger than the memory limit")
    shares = limits.cpu_shares
    if shares is not None and not MIN_CPU_SHARES <= shares <= MAX_CPU_SHARES:
        raise ValidationError(
            f"cpu shares must be between {MIN_CPU_SHARES} and {MAX_CPU_SHARES} (default 1024)"
        )
    return limits


def _check_cpus(cpus: float | None, host: HostCapacity) -> None:
    if cpus is None:
        return
    if cpus <= 0:
        raise ValidationError("cpus must be greater than 0")
    if cpus < MIN_CPUS:
        raise ValidationError(f"cpus must be at least {MIN_CPUS:g}")
    if cpus > host.cpus:
        raise ValidationError(f"cpus ({cpus:g}) cannot exceed the host's {host.cpus} CPUs")


def _check_memory(label: str, value: int | None, host: HostCapacity) -> None:
    if value is None:
        return
    if value < MIN_MEMORY_BYTES:
        raise ValidationError(f"{label} must be at least {format_size(MIN_MEMORY_BYTES)}")
    if value > host.memory_bytes:
        raise ValidationError(
            f"{label} ({format_size(value)}) cannot exceed the host's "
            f"{format_size(host.memory_bytes)} of memory"
        )
