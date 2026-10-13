"""Stored pipeline use cases (append-only versions)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from shiftcodes.domain.clock import Clock, utcnow
from shiftcodes.domain.enums import EnvironmentName
from shiftcodes.domain.errors import NotFoundError, ValidationError
from shiftcodes.pipeline import ValidationContext
from shiftcodes.repositories.database import UnitOfWork
from shiftcodes.repositories.models import PipelineDefinition
from shiftcodes.services.jenkinsfile_validator import JenkinsfileValidator
from shiftcodes.services.lookup import require_environment

MAX_JENKINSFILE_BYTES = 256 * 1024


class PipelineValidator(Protocol):
    """Hook for validating Jenkinsfile text; raise ``ValidationError`` to reject it."""

    def validate(self, jenkinsfile: str) -> None:
        """Validate ``jenkinsfile``."""
        ...


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
        created_by: int | None = None,
    ) -> PipelineDefinition:
        """Append a new version; earlier versions are never modified."""
        _check_size(jenkinsfile)
        (self._validator or self._default_validator(slug, name)).validate(jenkinsfile)
        with self._uow:
            environment = require_environment(self._uow, slug, name)
            version = self._uow.pipelines.latest_version(environment.id) + 1
            definition = PipelineDefinition(
                environment_id=environment.id,
                version=version,
                jenkinsfile=jenkinsfile,
                created_by=created_by,
                created_at=self._clock(),
            )
            return self._uow.pipelines.add(definition)

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

    def history(self, slug: str, name: EnvironmentName) -> Sequence[PipelineDefinition]:
        """Return all versions, newest first."""
        with self._uow:
            environment = require_environment(self._uow, slug, name)
            return self._uow.pipelines.list_history(environment.id)


def _check_size(jenkinsfile: str) -> None:
    if not jenkinsfile.strip():
        raise ValidationError("pipeline text must not be empty")
    if len(jenkinsfile.encode()) > MAX_JENKINSFILE_BYTES:
        raise ValidationError(f"pipeline text exceeds {MAX_JENKINSFILE_BYTES} bytes")
