"""CPU and memory limits of a container, within configured maxima and persisted."""

from __future__ import annotations

from dataclasses import replace

from backend.adapters.compose_overrides import ComposeOverrideStore, OverrideFileError
from backend.adapters.container_admin import ContainerEngine
from backend.adapters.docker_inspect import parse_container
from backend.adapters.errors import ContainerRuntimeError
from backend.containers.base import ContainerAuditor, Membership, TargetResolver
from backend.containers.config import ContainerConfig
from backend.containers.locks import EnvironmentLocks
from backend.domain.actor import Actor
from backend.domain.containers import ContainerInfo, LimitChange
from backend.domain.enums import EnvironmentName
from backend.domain.errors import ConflictError, ValidationError
from backend.domain.resources import parse_cpus, parse_memory

MIN_MEMORY_BYTES = 6 * 1024 * 1024  # Docker refuses anything below 6 MiB


def check_limits(change: LimitChange, config: ContainerConfig) -> tuple[float | None, int | None]:
    """Validate a change against the configured maxima; returns ``(cpus, memory_bytes)``."""
    if change.cpus is None and change.memory is None:
        raise ValidationError("set cpus, memory or both")
    cpus = None
    if change.cpus is not None:
        cpus = parse_cpus(f"{change.cpus:g}")
        if cpus > config.max_cpus:
            raise ValidationError(f"cpus may be at most {config.max_cpus:g}")
    memory = None
    if change.memory is not None:
        memory = parse_memory(change.memory)
        if memory < MIN_MEMORY_BYTES:
            raise ValidationError("memory must be at least 6m")
        if memory > config.max_memory_bytes:
            raise ValidationError(f"memory may be at most {config.max_memory_bytes} bytes")
    return cpus, memory


class LimitsService:
    """Applies limits live with ``docker update`` and keeps them in the managed override."""

    def __init__(  # noqa: PLR0913, PLR0917 - collaborators of one use case
        self,
        resolver: TargetResolver,
        engine: ContainerEngine,
        membership: Membership,
        store: ComposeOverrideStore,
        locks: EnvironmentLocks,
        auditor: ContainerAuditor,
        config: ContainerConfig,
    ) -> None:
        self._resolver = resolver
        self._engine = engine
        self._membership = membership
        self._store = store
        self._locks = locks
        self._auditor = auditor
        self._config = config

    def update(
        self,
        slug: str,
        env: EnvironmentName,
        container_id: str,
        change: LimitChange,
        actor: Actor,
    ) -> ContainerInfo:
        """Persist the limits in the override file, apply them, and return the container."""
        cpus, memory = check_limits(change, self._config)
        target = self._resolver.resolve(slug, env)
        with self._locks.hold(str(target.key), timeout=self._config.lock_timeout):
            record, service = self._membership.require_service(target, container_id)
            before = parse_container(record)
            snapshot = self._store.snapshot(target.key)
            details: dict[str, object] = {
                "service": service,
                "cpus": cpus,
                "memory_bytes": memory,
                "previous_cpus": before.cpus,
                "previous_memory_bytes": before.memory_bytes,
            }
            try:
                current = self._store.load(target.key)
                existing = current.for_service(service)
                merged = replace(
                    existing,
                    cpus=cpus if cpus is not None else existing.cpus,
                    memory_bytes=memory if memory is not None else existing.memory_bytes,
                )
                self._store.save(target.key, current.with_service(service, merged))
                self._engine.update_limits(container_id, cpus, memory)
            except (OverrideFileError, ContainerRuntimeError) as error:
                self._store.restore(target.key, snapshot)
                self._auditor.record(
                    actor,
                    "container.limits.update",
                    target,
                    container_id,
                    {**details, "outcome": "failed"},
                )
                raise ConflictError(str(error)) from error
            self._auditor.record(
                actor,
                "container.limits.update",
                target,
                container_id,
                {**details, "outcome": "applied"},
            )
            return parse_container(self._membership.require(target, container_id))
