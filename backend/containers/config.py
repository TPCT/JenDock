"""Bounds and timings of container management, derived from settings."""

from __future__ import annotations

from dataclasses import dataclass

from backend.config import Settings
from backend.domain.resources import parse_cpus, parse_memory


@dataclass(frozen=True, slots=True)
class ContainerConfig:
    """Limits a user may set and how long operations wait."""

    max_cpus: float = 8.0
    max_memory_bytes: int = 16 * 1024**3
    verify_timeout: float = 60.0
    lock_timeout: float = 30.0
    # A recreated container must stay up this long to count as running (catches crash loops).
    settle_seconds: float = 3.0
    poll_seconds: float = 0.5

    @classmethod
    def from_settings(cls, settings: Settings) -> ContainerConfig:
        """Take maxima and timeouts from ``Settings``."""
        return cls(
            max_cpus=parse_cpus(settings.managed_max_cpus),
            max_memory_bytes=parse_memory(settings.managed_max_memory),
            verify_timeout=settings.container_verify_timeout,
            lock_timeout=settings.container_lock_timeout,
        )
