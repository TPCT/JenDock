"""Shared foundation of container management: target resolution, membership and audit."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from backend.adapters.container_admin import ContainerEngine
from backend.adapters.docker_inspect import compose_project_of, service_of
from backend.adapters.errors import ContainerNotFoundError, ContainerRuntimeError
from backend.domain.actor import Actor
from backend.domain.audit import AuditTarget
from backend.domain.clock import Clock, utcnow
from backend.domain.containers import EnvironmentKey, validate_container_id
from backend.domain.enums import EnvironmentName
from backend.domain.errors import ConflictError, NotFoundError, ValidationError
from backend.repositories.database import SessionFactory, UnitOfWork
from backend.services.audit import AuditService
from backend.services.lookup import require_environment

Record = Mapping[str, object]


@dataclass(frozen=True, slots=True)
class EnvironmentTarget:
    """An environment's compose project, resolved from the database."""

    key: EnvironmentKey
    environment_id: int
    compose_project: str
    compose_files: tuple[str, ...]
    compose_env_files: tuple[str, ...] = ()


class TargetResolver:
    """Finds the compose project of ``slug/env``; only containers of it are manageable."""

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory

    def resolve(self, slug: str, env: EnvironmentName) -> EnvironmentTarget:
        """The target, or ``NotFoundError`` / ``ValidationError`` when it cannot be managed."""
        uow = UnitOfWork(self._session_factory)
        with uow:
            environment = require_environment(uow, slug, env)
            project = environment.compose_project
            files = tuple(environment.compose_files)
            env_files = tuple(environment.compose_env_files)
            environment_id = environment.id
        if not project:
            raise ValidationError("this environment has no compose project configured")
        return EnvironmentTarget(
            EnvironmentKey(slug, env), environment_id, project, files, env_files
        )


class Membership:
    """Identity pinning: a container is addressed by its exact id and must belong to the project."""

    def __init__(self, engine: ContainerEngine) -> None:
        self._engine = engine

    def require(self, target: EnvironmentTarget, container_id: str) -> Record:
        """Inspect ``container_id``; it must exist, match the id exactly and be in the project.

        Containers of other projects look exactly like missing ones.
        """
        validate_container_id(container_id)
        try:
            record = self._engine.inspect(container_id)
        except ContainerNotFoundError as error:
            raise NotFoundError("container not found") from error
        except ContainerRuntimeError as error:
            raise ConflictError(str(error)) from error
        if record.get("Id") != container_id or compose_project_of(record) != target.compose_project:
            raise NotFoundError("container not found")
        if _going_away(record):
            raise NotFoundError("container not found")
        return record

    def require_service(self, target: EnvironmentTarget, container_id: str) -> tuple[Record, str]:
        """Like ``require`` and also return the compose service name."""
        record = self.require(target, container_id)
        service = service_of(record)
        if service is None:
            raise ConflictError("container has no compose service label")
        return record, service


def _going_away(record: Record) -> bool:
    """A container whose removal has started can no longer be acted on."""
    state = record.get("State")
    if not isinstance(state, dict):
        return False
    return bool(state.get("Dead") or state.get("RemovalInProgress"))


class ContainerAuditor:
    """Records container-management events; sensitive detail keys are rejected by the audit rule."""

    def __init__(self, session_factory: SessionFactory, clock: Clock = utcnow) -> None:
        self._session_factory = session_factory
        self._clock = clock

    def record(
        self,
        actor: Actor,
        action: str,
        target: EnvironmentTarget,
        container_id: str,
        details: Mapping[str, object] | None = None,
    ) -> None:
        """Store one event about ``container_id``; details never carry values or output."""
        service = AuditService(UnitOfWork(self._session_factory), self._clock)
        merged: dict[str, object] = {
            "project": target.key.project,
            "environment": target.key.environment.value,
            **(details or {}),
        }
        service.record(actor, action, AuditTarget("container", container_id), merged)

    def record_environment(
        self,
        actor: Actor,
        action: str,
        target: EnvironmentTarget,
        details: Mapping[str, object] | None = None,
    ) -> None:
        """Store an event about an environment-wide operation."""
        merged: dict[str, object] = {
            "project": target.key.project,
            "environment": target.key.environment.value,
            **(details or {}),
        }
        AuditService(UnitOfWork(self._session_factory), self._clock).record(
            actor, action, AuditTarget("environment", str(target.environment_id)), merged
        )
