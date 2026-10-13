"""Execution settings, taken from ``Settings`` once."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from backend.config import Settings
from backend.domain.resources import parse_cpus, parse_memory

HEALTH_INTERVAL = 2.0
HEALTH_ATTEMPT_TIMEOUT = 10.0


@dataclass(frozen=True, slots=True)
class ExecutionConfig:
    """Limits and tunables of pipeline execution."""

    log_max_bytes: int = 5 * 1024**2
    parallel_workers: int = 2
    max_concurrent_runs: int = 1
    container_memory: str = "2g"
    container_cpus: float = 2.0
    build_memory: str = "2g"
    build_network: str | None = None
    step_timeout_seconds: float = 3600.0
    queue_poll_interval: float = 1.0
    cancel_poll_interval: float = 1.0
    health_interval: float = HEALTH_INTERVAL
    # Containers that mount the workspace run as the worker's own user so that every file
    # they create can be deleted by the worker afterwards.
    container_user: str = field(default_factory=lambda: f"{os.getuid()}:{os.getgid()}")

    @property
    def container_memory_bytes(self) -> int:
        """The memory ceiling of a step container in bytes."""
        return parse_memory(self.container_memory)

    @classmethod
    def from_settings(cls, settings: Settings) -> ExecutionConfig:
        """Build the config from application settings."""
        return cls(
            log_max_bytes=settings.step_log_max_bytes,
            parallel_workers=settings.parallel_workers,
            max_concurrent_runs=settings.max_concurrent_runs,
            container_memory=settings.container_memory,
            container_cpus=parse_cpus(settings.container_cpus),
            build_memory=settings.build_memory,
            build_network=settings.build_network,
            step_timeout_seconds=float(settings.step_timeout_seconds),
            queue_poll_interval=settings.queue_poll_interval,
            cancel_poll_interval=settings.cancel_poll_interval,
        )
