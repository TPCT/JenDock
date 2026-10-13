"""The saved resource limits of an environment's services (the database side)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from backend.containers.base import EnvironmentTarget
from backend.domain.actor import Actor
from backend.domain.clock import Clock, utcnow
from backend.domain.enums import ActorType, EnvironmentName
from backend.domain.resources import ResourceLimits
from backend.repositories.database import SessionFactory, UnitOfWork
from backend.repositories.models import ResourceLimit
from backend.services.lookup import require_environment


@dataclass(frozen=True, slots=True)
class SavedLimits:
    """One service's saved limits and when they last changed."""

    service: str
    limits: ResourceLimits
    updated_at: datetime


def _limits_of(row: ResourceLimit) -> ResourceLimits:
    return ResourceLimits(
        cpus=row.cpus,
        memory_bytes=row.memory_bytes,
        memory_reservation_bytes=row.memory_reservation_bytes,
        cpu_shares=row.cpu_shares,
    )


class ResourceLimitStore:
    """Reads and writes ``resource_limits``; each call is one transaction."""

    def __init__(self, session_factory: SessionFactory, clock: Clock = utcnow) -> None:
        self._session_factory = session_factory
        self._clock = clock

    def saved(self, environment_id: int) -> list[SavedLimits]:
        """Every saved service of the environment, ordered by service."""
        uow = UnitOfWork(self._session_factory)
        with uow:
            rows = uow.resource_limits.list_for_environment(environment_id)
            return [SavedLimits(row.service, _limits_of(row), row.updated_at) for row in rows]

    def limits(self, environment_id: int) -> Mapping[str, ResourceLimits]:
        """Service to limits of the environment."""
        return {item.service: item.limits for item in self.saved(environment_id)}

    def limits_of(self, slug: str, environment: EnvironmentName) -> Mapping[str, ResourceLimits]:
        """Like ``limits`` for the environment named by project slug and name."""
        uow = UnitOfWork(self._session_factory)
        with uow:
            found = require_environment(uow, slug, environment)
            rows = uow.resource_limits.list_for_environment(found.id)
            return {row.service: _limits_of(row) for row in rows}

    def configured_services(self, target: EnvironmentTarget) -> list[str]:
        """The services the environment lists for deployment."""
        uow = UnitOfWork(self._session_factory)
        with uow:
            found = uow.environments.get(target.environment_id)
            return list(found.services) if found is not None else []

    def get(self, environment_id: int, service: str) -> SavedLimits | None:
        """The saved limits of one service."""
        uow = UnitOfWork(self._session_factory)
        with uow:
            row = uow.resource_limits.get(environment_id, service)
            return SavedLimits(row.service, _limits_of(row), row.updated_at) if row else None

    def save(
        self, environment_id: int, service: str, limits: ResourceLimits, actor: Actor
    ) -> SavedLimits:
        """Create or replace the limits of a service."""
        uow = UnitOfWork(self._session_factory)
        with uow:
            row = uow.resource_limits.get(environment_id, service)
            if row is None:
                row = uow.resource_limits.add(
                    ResourceLimit(environment_id=environment_id, service=service)
                )
            row.cpus = limits.cpus
            row.memory_bytes = limits.memory_bytes
            row.memory_reservation_bytes = limits.memory_reservation_bytes
            row.cpu_shares = limits.cpu_shares
            user_id = _user_id(actor)
            row.updated_by = user_id if user_id and uow.users.get(user_id) else None
            row.updated_at = self._clock()
            return SavedLimits(row.service, _limits_of(row), row.updated_at)

    def delete(self, environment_id: int, service: str) -> bool:
        """Remove the limits of a service; whether there were any."""
        uow = UnitOfWork(self._session_factory)
        with uow:
            row = uow.resource_limits.get(environment_id, service)
            if row is None:
                return False
            uow.resource_limits.delete(row)
            return True


def _user_id(actor: Actor) -> int | None:
    if actor.type is ActorType.USER and actor.id is not None and actor.id.isdigit():
        return int(actor.id)
    return None
