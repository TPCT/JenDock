"""Stored pipeline use cases (append-only versions)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from sqlalchemy.exc import IntegrityError

from backend.domain.actor import Actor
from backend.domain.audit import AuditEntry, AuditTarget
from backend.domain.clock import Clock, utcnow
from backend.domain.enums import ActorType, EnvironmentName
from backend.domain.errors import ConflictError, NotFoundError, ValidationError
from backend.pipeline import ValidationContext
from backend.repositories.database import UnitOfWork
from backend.repositories.models import PipelineDefinition
from backend.services.audit import add_audit_event
from backend.services.jenkinsfile_validator import JenkinsfileValidator
from backend.services.lookup import require_environment

MAX_JENKINSFILE_BYTES = 256 * 1024


class PipelineValidator(Protocol):
    """Hook for validating Jenkinsfile text; raise ``ValidationError`` to reject it."""

    def validate(self, jenkinsfile: str) -> None:
        """Validate ``jenkinsfile``."""
        ...


@dataclass(frozen=True, slots=True)
class StoredPipeline:
    """One stored version with the author's user name (``None`` for tokens and deleted users)."""

    version: int
    created_at: datetime
    created_by: str | None
    jenkinsfile: str


class PipelineService:
    """Save and read the pipeline stored in the platform for an environment.

    Without an explicit ``validator`` every save is checked by the Jenkinsfile engine, using the
    secrets of the target environment for ``credentials('NAME')``.
    """

    def __init__(
        self,
        uow: UnitOfWork,
        validator: PipelineValidator | None = None,
        clock: Clock = utcnow,
    ) -> None:
        self._uow = uow
        self._validator = validator
        self._clock = clock

    def save(
        self,
        slug: str,
        name: EnvironmentName,
        jenkinsfile: str,
        *,
        actor: Actor | None = None,
        base_version: int | None = None,
    ) -> PipelineDefinition:
        """Append a new version; earlier versions are never modified.

        With ``base_version`` the save only succeeds when it is still the latest version
        (optimistic concurrency, ``ConflictError`` otherwise; two racing saves cannot both win
        because (environment, version) is unique). With ``actor`` the save is audited.
        """
        _check_size(jenkinsfile)
        (self._validator or self._default_validator(slug, name)).validate(jenkinsfile)
        try:
            return self._append(slug, name, jenkinsfile, actor, base_version)
        except IntegrityError as error:
            raise ConflictError("the pipeline was changed by someone else; reload it") from error

    def _append(
        self,
        slug: str,
        name: EnvironmentName,
        jenkinsfile: str,
        actor: Actor | None,
        base_version: int | None,
    ) -> PipelineDefinition:
        with self._uow:
            environment = require_environment(self._uow, slug, name)
            latest = self._uow.pipelines.latest_version(environment.id)
            if base_version is not None and base_version != latest:
                raise ConflictError(
                    f"the pipeline is at version {latest}, not {base_version}; reload it"
                )
            definition = PipelineDefinition(
                environment_id=environment.id,
                version=latest + 1,
                jenkinsfile=jenkinsfile,
                created_by=_user_id(actor),
                created_at=self._clock(),
            )
            self._uow.pipelines.add(definition)
            if actor is not None:
                details: dict[str, object] = {
                    "version": definition.version,
                    "bytes": len(jenkinsfile.encode()),
                }
                target = AuditTarget("environment", str(environment.id))
                entry = AuditEntry(actor, "pipeline.save", target, details)
                add_audit_event(self._uow, entry, self._clock)
            return definition

    def _default_validator(self, slug: str, name: EnvironmentName) -> PipelineValidator:
        """Jenkinsfile validator that knows the secrets of the environment."""
        with self._uow:
            environment = require_environment(self._uow, slug, name)
            secrets = self._uow.secrets.list_for_environment(environment.id)
            context = ValidationContext(frozenset(secret.name for secret in secrets))
        return JenkinsfileValidator(context)

    def get_active(self, slug: str, name: EnvironmentName) -> PipelineDefinition:
        """Return the latest version; ``NotFoundError`` when none was saved."""
        with self._uow:
            environment = require_environment(self._uow, slug, name)
            latest = self._uow.pipelines.get_latest(environment.id)
            if latest is None:
                raise NotFoundError(f"no pipeline stored for {slug}/{name.value}")
            return latest

    def current_version(self, slug: str, name: EnvironmentName) -> int:
        """The latest stored version number, 0 when nothing is stored."""
        with self._uow:
            environment = require_environment(self._uow, slug, name)
            return self._uow.pipelines.latest_version(environment.id)

    def latest_stored(self, slug: str, name: EnvironmentName) -> StoredPipeline | None:
        """The latest stored version with its author, or ``None``."""
        with self._uow:
            environment = require_environment(self._uow, slug, name)
            latest = self._uow.pipelines.get_latest(environment.id)
            return None if latest is None else self._stored(latest)

    def stored_version(self, slug: str, name: EnvironmentName, version: int) -> StoredPipeline:
        """One stored version with its author; ``NotFoundError`` for an unknown number."""
        with self._uow:
            environment = require_environment(self._uow, slug, name)
            found = self._uow.pipelines.get_version(environment.id, version)
            if found is None:
                raise NotFoundError(f"no pipeline version {version} for {slug}/{name.value}")
            return self._stored(found)

    def stored_history(self, slug: str, name: EnvironmentName) -> list[StoredPipeline]:
        """All versions with their authors, newest first."""
        with self._uow:
            environment = require_environment(self._uow, slug, name)
            history = self._uow.pipelines.list_history(environment.id)
            return [self._stored(definition) for definition in history]

    def _stored(self, definition: PipelineDefinition) -> StoredPipeline:
        """Must be called inside the unit of work (looks the author up)."""
        author = None
        if definition.created_by is not None:
            user = self._uow.users.get(definition.created_by)
            author = user.username if user is not None else None
        return StoredPipeline(
            definition.version, definition.created_at, author, definition.jenkinsfile
        )

    def history(self, slug: str, name: EnvironmentName) -> Sequence[PipelineDefinition]:
        """Return all versions, newest first."""
        with self._uow:
            environment = require_environment(self._uow, slug, name)
            return self._uow.pipelines.list_history(environment.id)


def _user_id(actor: Actor | None) -> int | None:
    """The user id of a browser-session actor (tokens and the system have no user row)."""
    if actor is None or actor.type is not ActorType.USER or actor.id is None:
        return None
    return int(actor.id)


def _check_size(jenkinsfile: str) -> None:
    if not jenkinsfile.strip():
        raise ValidationError("pipeline text must not be empty")
    if len(jenkinsfile.encode()) > MAX_JENKINSFILE_BYTES:
        raise ValidationError(f"pipeline text exceeds {MAX_JENKINSFILE_BYTES} bytes")
