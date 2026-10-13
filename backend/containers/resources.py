"""CPU and memory limits per compose service: saved, rendered into the override, applied live.

The database row (``resource_limits``) is the single source of truth. Saving a service's limits

1. validates them against what the Docker host has,
2. renders the managed compose override (``resource-limits.yml``) so every later deploy and
   recreate of the service keeps them, and stores the row,
3. optionally runs ``docker update`` on the service's running containers ("apply now"), which
   needs no restart.

Docker cannot take a limit back off a running container (``docker update`` treats 0 as
"unchanged"), so removing a limit and applying it live raises the running containers to the
host's capacity; the next deploy or recreate drops the limit entirely.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime

from backend.adapters.compose_overrides import ComposeOverrideStore, OverrideFileError
from backend.adapters.container_admin import ContainerEngine, LiveUpdate
from backend.adapters.docker_inspect import parse_container
from backend.adapters.errors import AdapterError, ContainerRuntimeError
from backend.adapters.resource_overrides import ResourceOverrideStore
from backend.containers.base import ContainerAuditor, EnvironmentTarget, TargetResolver
from backend.containers.config import ContainerConfig
from backend.containers.locks import EnvironmentLocks
from backend.containers.resource_store import ResourceLimitStore, SavedLimits
from backend.domain.actor import Actor
from backend.domain.containers import ContainerInfo, EnvironmentKey
from backend.domain.enums import EnvironmentName
from backend.domain.errors import ConflictError, NotFoundError
from backend.domain.resources import (
    HostCapacity,
    ResourceLimits,
    validate_limits,
    validate_service_name,
)

CAPACITY_TTL_SECONDS = 60.0
DEFAULT_CPU_SHARES = 1024  # Docker's default weight, used to take a custom weight back
UNLIMITED_SWAP = -1
NOT_RUNNING_NOTE = "No container of this service is running; the limits apply at the next deploy."
LIFTED_NOTE = (
    "Docker cannot remove a limit from a running container, so it was raised to the host's "
    "capacity; the next deploy removes it completely."
)
RESERVATION_NOTE = (
    "The memory reservation is a soft limit that Docker cannot remove from a running "
    "container; the next deploy removes it."
)


@dataclass(frozen=True, slots=True)
class ServiceResources:
    """A service of the environment with its saved limits (empty when none are saved)."""

    service: str
    limits: ResourceLimits
    saved: bool
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ResourceOverview:
    """The host's capacity and every service's saved limits."""

    host: HostCapacity
    services: tuple[ServiceResources, ...]


@dataclass(frozen=True, slots=True)
class AppliedContainer:
    """The outcome of ``docker update`` on one running container."""

    id: str
    name: str
    ok: bool
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ResourceChange:
    """What a save, removal or apply did."""

    service: str
    limits: ResourceLimits
    saved: bool
    applied: tuple[AppliedContainer, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ContainerUsage:
    """Live usage of one running container against its applied limits (``None``: unlimited)."""

    id: str
    name: str
    service: str | None
    cpu_percent: float
    memory_used: int
    memory_percent: float
    cpu_limit: float | None
    memory_limit: int | None


@dataclass(frozen=True, slots=True)
class ResourceUsage:
    """The host's capacity and the usage of every running container of the environment."""

    host: HostCapacity
    containers: tuple[ContainerUsage, ...]


class HostCapacityReader:
    """The Docker host's CPUs and memory, read from ``docker info`` and cached briefly."""

    def __init__(
        self, engine: ContainerEngine, monotonic: Callable[[], float] = time.monotonic
    ) -> None:
        self._engine = engine
        self._monotonic = monotonic
        self._cached: tuple[float, HostCapacity] | None = None

    def read(self) -> HostCapacity:
        """The capacity; ``ConflictError`` when Docker cannot say."""
        now = self._monotonic()
        if self._cached is not None and now - self._cached[0] < CAPACITY_TTL_SECONDS:
            return self._cached[1]
        try:
            capacity = self._engine.host_capacity()
        except AdapterError as error:
            raise ConflictError(str(error)) from error
        self._cached = (now, capacity)
        return capacity


class ResourceOverrideFiles:
    """The resource-limits override as a managed compose file of an environment.

    Implements ``OverrideFiles`` for service recreation and serves the deployer's extra-files
    hook; both render the file from the database first, so it can never be stale.
    """

    def __init__(self, store: ResourceLimitStore, files: ResourceOverrideStore) -> None:
        self._store = store
        self._files = files

    def render(self, key: EnvironmentKey) -> list[str]:
        """Write the current limits of the environment and return the file (if any)."""
        self._files.write(key, self._store.limits_of(key.project, key.environment))
        return self._files.files(key)

    def paths(self, target: EnvironmentTarget) -> list[str]:
        """``OverrideFiles``: the file of the environment of a container operation."""
        return self.render(target.key)


def live_update(
    new: ResourceLimits, previous: ResourceLimits | None, host: HostCapacity
) -> tuple[LiveUpdate, tuple[str, ...]]:
    """The ``docker update`` values that make a running container follow ``new``.

    A limit that ``previous`` had and ``new`` lacks is lifted to the host's capacity (the closest
    Docker allows). Memory always travels with its swap limit in the same call, so lowering or
    raising the memory never fails against the swap the container already has.
    """
    was = previous or ResourceLimits()
    notes: list[str] = []
    cpus = new.cpus
    if cpus is None and was.cpus is not None:
        cpus = float(host.cpus)
        notes.append(LIFTED_NOTE)
    memory, swap = new.memory_bytes, new.memory_swap_bytes
    if memory is None and was.memory_bytes is not None:
        memory, swap = host.memory_bytes, UNLIMITED_SWAP
        if LIFTED_NOTE not in notes:
            notes.append(LIFTED_NOTE)
    shares = new.cpu_shares
    if shares is None and was.cpu_shares is not None:
        shares = DEFAULT_CPU_SHARES
    if new.memory_reservation_bytes is None and was.memory_reservation_bytes is not None:
        notes.append(RESERVATION_NOTE)
    update = LiveUpdate(
        cpus=cpus,
        memory_bytes=memory,
        memory_swap_bytes=swap,
        memory_reservation_bytes=new.memory_reservation_bytes,
        cpu_shares=shares,
    )
    return update, tuple(notes)


def _entry(name: str, saved: SavedLimits | None) -> ServiceResources:
    if saved is None:
        return ServiceResources(name, ResourceLimits(), saved=False)
    return ServiceResources(name, saved.limits, saved=True, updated_at=saved.updated_at)


class ResourceService:
    """Saves, removes, applies and reports the resource limits of an environment's services."""

    def __init__(  # noqa: PLR0913, PLR0917 - collaborators of one use case
        self,
        resolver: TargetResolver,
        engine: ContainerEngine,
        store: ResourceLimitStore,
        files: ResourceOverrideStore,
        legacy: ComposeOverrideStore,
        locks: EnvironmentLocks,
        auditor: ContainerAuditor,
        config: ContainerConfig,
        host: HostCapacityReader,
    ) -> None:
        self._resolver = resolver
        self._engine = engine
        self._store = store
        self._files = files
        self._legacy = legacy
        self._locks = locks
        self._auditor = auditor
        self._config = config
        self._host = host

    # --- reads --------------------------------------------------------------------

    def overview(self, slug: str, env: EnvironmentName) -> ResourceOverview:
        """Host capacity and the limits of every known service (containers, saved, configured)."""
        target = self._resolver.resolve(slug, env)
        host = self._host.read()
        saved = {item.service: item for item in self._store.saved(target.environment_id)}
        names = set(saved) | set(self._store.configured_services(target))
        names |= {info.service for info in self._containers(target) if info.service}
        return ResourceOverview(
            host, tuple(_entry(name, saved.get(name)) for name in sorted(names))
        )

    def usage(self, slug: str, env: EnvironmentName) -> ResourceUsage:
        """A ``docker stats --no-stream`` of every running container with the host's capacity."""
        target = self._resolver.resolve(slug, env)
        host = self._host.read()
        running = {info.id: info for info in self._containers(target) if info.running}
        if not running:
            return ResourceUsage(host, ())
        try:
            samples = self._engine.stats(list(running))
        except AdapterError as error:
            raise ConflictError(str(error)) from error
        usage = [
            ContainerUsage(
                id=info.id,
                name=info.name,
                service=info.service,
                cpu_percent=sample.cpu_percent,
                memory_used=sample.memory_used,
                memory_percent=sample.memory_percent,
                cpu_limit=info.cpus,
                memory_limit=info.memory_bytes,
            )
            for sample in samples
            if (info := running.get(sample.id)) is not None
        ]
        usage.sort(key=lambda item: (item.service or "", item.name))
        return ResourceUsage(host, tuple(usage))

    # --- writes -------------------------------------------------------------------

    def save(  # noqa: PLR0913 - one command
        self,
        slug: str,
        env: EnvironmentName,
        service: str,
        limits: ResourceLimits,
        actor: Actor,
        *,
        apply_now: bool = False,
    ) -> ResourceChange:
        """Validate and persist ``limits`` for ``service``; optionally apply them live."""
        validate_service_name(service)
        target = self._resolver.resolve(slug, env)
        validate_limits(limits, self._host.read())
        with self._locks.hold(str(target.key), timeout=self._config.lock_timeout):
            self._require_service(target, service)
            previous = self._store.get(target.environment_id, service)
            was = previous.limits if previous else None
            self._persist(target, service, limits, actor)
            applied, notes = self._maybe_apply(target, service, limits, was, apply_now)
            self._audit(actor, "container.resources.update", target, service, limits, was, applied)
            return ResourceChange(service, limits, True, applied, notes)

    def remove(
        self,
        slug: str,
        env: EnvironmentName,
        service: str,
        actor: Actor,
        *,
        apply_now: bool = False,
    ) -> ResourceChange:
        """Delete the limits of ``service``; optionally lift them on the running containers."""
        validate_service_name(service)
        target = self._resolver.resolve(slug, env)
        with self._locks.hold(str(target.key), timeout=self._config.lock_timeout):
            previous = self._store.get(target.environment_id, service)
            if previous is None:
                raise NotFoundError("no resource limits are saved for this service")
            self._persist(target, service, None, actor)
            empty = ResourceLimits()
            applied, notes = self._maybe_apply(target, service, empty, previous.limits, apply_now)
            self._audit(
                actor,
                "container.resources.remove",
                target,
                service,
                empty,
                previous.limits,
                applied,
            )
            return ResourceChange(service, empty, False, applied, notes)

    def apply(self, slug: str, env: EnvironmentName, service: str, actor: Actor) -> ResourceChange:
        """Copy the saved limits onto the running containers of ``service`` (no restart)."""
        validate_service_name(service)
        target = self._resolver.resolve(slug, env)
        with self._locks.hold(str(target.key), timeout=self._config.lock_timeout):
            saved = self._store.get(target.environment_id, service)
            if saved is None:
                raise NotFoundError("no resource limits are saved for this service")
            applied, notes = self._maybe_apply(target, service, saved.limits, None, True)
            self._audit(
                actor, "container.resources.apply", target, service, saved.limits, None, applied
            )
            return ResourceChange(service, saved.limits, True, applied, notes)

    # --- internals ----------------------------------------------------------------

    def _containers(self, target: EnvironmentTarget) -> list[ContainerInfo]:
        try:
            records = self._engine.list_project(target.compose_project)
        except ContainerRuntimeError as error:
            raise ConflictError(str(error)) from error
        return [parse_container(record) for record in records]

    def _require_service(self, target: EnvironmentTarget, service: str) -> None:
        known = {info.service for info in self._containers(target)}
        known |= set(self._store.configured_services(target))
        known |= {item.service for item in self._store.saved(target.environment_id)}
        if service not in known:
            raise NotFoundError(f"service not found in this environment: {service}")

    def _persist(
        self,
        target: EnvironmentTarget,
        service: str,
        limits: ResourceLimits | None,
        actor: Actor,
    ) -> None:
        """Write the override file for the new state, then the row; undo the file on failure."""
        before = self._store.limits(target.environment_id)
        after = {name: item for name, item in before.items() if name != service}
        if limits is not None:
            after[service] = limits
        legacy = self._legacy.snapshot(target.key)
        try:
            self._files.write(target.key, after)
            if limits is None:
                self._store.delete(target.environment_id, service)
            else:
                self._store.save(target.environment_id, service, limits, actor)
            self._drop_legacy_limits(target.key, service)
        except OverrideFileError as error:
            self._undo(target.key, before, legacy)
            raise ConflictError(str(error)) from error
        except Exception:
            self._undo(target.key, before, legacy)
            raise

    def _drop_legacy_limits(self, key: EnvironmentKey, service: str) -> None:
        """The older per-container limits live in the container-management override file; the
        saved limits replace them so there is one source of truth for the service."""
        current = self._legacy.load(key)
        existing = current.for_service(service)
        if existing.cpus is None and existing.memory_bytes is None:
            return
        cleaned = replace(existing, cpus=None, memory_bytes=None)
        self._legacy.save(key, current.with_service(service, cleaned))

    def _undo(
        self, key: EnvironmentKey, before: Mapping[str, ResourceLimits], legacy: bytes | None
    ) -> None:
        with contextlib.suppress(OverrideFileError):
            self._files.write(key, before)
        with contextlib.suppress(OverrideFileError):
            self._legacy.restore(key, legacy)

    def _maybe_apply(
        self,
        target: EnvironmentTarget,
        service: str,
        new: ResourceLimits,
        previous: ResourceLimits | None,
        apply_now: bool,
    ) -> tuple[tuple[AppliedContainer, ...], tuple[str, ...]]:
        if not apply_now:
            return (), ()
        update, notes = live_update(new, previous, self._host.read())
        running = [i for i in self._containers(target) if i.service == service and i.running]
        if not running:
            return (), (NOT_RUNNING_NOTE,)
        results: list[AppliedContainer] = []
        for info in running:
            try:
                self._engine.update_resources(info.id, update)
            except AdapterError as error:
                results.append(AppliedContainer(info.id, info.name, ok=False, error=str(error)))
            else:
                results.append(AppliedContainer(info.id, info.name, ok=True))
        return tuple(results), notes

    def _audit(  # noqa: PLR0913, PLR0917 - one event
        self,
        actor: Actor,
        action: str,
        target: EnvironmentTarget,
        service: str,
        limits: ResourceLimits,
        previous: ResourceLimits | None,
        applied: tuple[AppliedContainer, ...],
    ) -> None:
        details: dict[str, object] = {
            "service": service,
            "cpus": limits.cpus,
            "memory_bytes": limits.memory_bytes,
            "memory_reservation_bytes": limits.memory_reservation_bytes,
            "cpu_shares": limits.cpu_shares,
            "previous_cpus": previous.cpus if previous else None,
            "previous_memory_bytes": previous.memory_bytes if previous else None,
            "applied_containers": sum(1 for item in applied if item.ok),
            "failed_containers": sum(1 for item in applied if not item.ok),
        }
        self._auditor.record(actor, action, target, service, details)
