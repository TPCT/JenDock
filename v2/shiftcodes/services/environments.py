"""Environment and stored-pipeline use cases."""

from __future__ import annotations

from dataclasses import dataclass

from shiftcodes.domain.clock import Clock, utcnow
from shiftcodes.domain.enums import EnvironmentName
from shiftcodes.domain.values import (
    BranchName,
    validate_compose_files,
    validate_compose_project,
    validate_service_names,
)
from shiftcodes.repositories.database import UnitOfWork
from shiftcodes.repositories.models import Environment
from shiftcodes.services.lookup import require_environment


@dataclass(frozen=True, slots=True)
class EnvironmentUpdate:
    """Partial change set; ``None`` means unchanged.

    ``clear_compose_project`` removes the compose project name.
    """

    branch: str | None = None
    auto_deploy: bool | None = None
    compose_project: str | None = None
    clear_compose_project: bool = False
    compose_files: list[str] | None = None
    services: list[str] | None = None


class EnvironmentService:
    """Read and configure an environment."""

    def __init__(self, uow: UnitOfWork, clock: Clock = utcnow) -> None:
        self._uow = uow
        self._clock = clock

    def get(self, slug: str, name: EnvironmentName) -> Environment:
        """Return the environment ``name`` of project ``slug``."""
        with self._uow:
            return require_environment(self._uow, slug, name)

    def update(self, slug: str, name: EnvironmentName, change: EnvironmentUpdate) -> Environment:
        """Validate and apply ``change``; nothing is stored when validation fails."""
        validated = _validate_update(change)
        with self._uow:
            environment = require_environment(self._uow, slug, name)
            _apply_update(environment, validated)
            environment.updated_at = self._clock()
            return environment


def _validate_update(change: EnvironmentUpdate) -> EnvironmentUpdate:
    return EnvironmentUpdate(
        branch=str(BranchName(change.branch)) if change.branch is not None else None,
        auto_deploy=change.auto_deploy,
        compose_project=validate_compose_project(change.compose_project),
        clear_compose_project=change.clear_compose_project,
        compose_files=validate_compose_files(change.compose_files)
        if change.compose_files is not None
        else None,
        services=validate_service_names(change.services) if change.services is not None else None,
    )


def _apply_update(environment: Environment, change: EnvironmentUpdate) -> None:
    if change.branch is not None:
        environment.branch = change.branch
    if change.auto_deploy is not None:
        environment.auto_deploy = change.auto_deploy
    if change.clear_compose_project:
        environment.compose_project = None
    elif change.compose_project is not None:
        environment.compose_project = change.compose_project
    if change.compose_files is not None:
        environment.compose_files = change.compose_files
    if change.services is not None:
        environment.services = change.services
