"""Environment and stored-pipeline use cases."""

from __future__ import annotations

from dataclasses import dataclass

from backend.domain.actor import Actor
from backend.domain.audit import AuditEntry, AuditTarget
from backend.domain.clock import Clock, utcnow
from backend.domain.enums import EnvironmentName
from backend.domain.errors import ValidationError
from backend.domain.values import (
    BranchName,
    validate_compose_env_files,
    validate_compose_files,
    validate_compose_project,
    validate_service_names,
)
from backend.repositories.database import UnitOfWork
from backend.repositories.models import Environment
from backend.services.audit import add_audit_event
from backend.services.lookup import require_environment


@dataclass(frozen=True, slots=True)
class EnvironmentUpdate:
    """Partial change set; ``None`` means unchanged.

    ``clear_compose_project`` removes the compose project name.
    """

    branch: str | None = None
    auto_deploy: bool | None = None
    auto_stop_hours: int | None = None
    set_auto_stop_hours: bool = False
    compose_project: str | None = None
    clear_compose_project: bool = False
    compose_files: list[str] | None = None
    compose_env_files: list[str] | None = None
    services: list[str] | None = None
    include_in_backups: bool | None = None


class EnvironmentService:
    """Read and configure an environment."""

    def __init__(self, uow: UnitOfWork, clock: Clock = utcnow) -> None:
        self._uow = uow
        self._clock = clock

    def get(self, slug: str, name: EnvironmentName) -> Environment:
        """Return the environment ``name`` of project ``slug``."""
        with self._uow:
            return require_environment(self._uow, slug, name)

    def update(
        self,
        slug: str,
        name: EnvironmentName,
        change: EnvironmentUpdate,
        actor: Actor | None = None,
    ) -> Environment:
        """Validate and apply ``change``; nothing is stored when validation fails.

        The two environments of a project must track different branches. With an ``actor``
        the change is audited (``environment.update``) in the same transaction.
        """
        validated = _validate_update(change)
        with self._uow:
            environment = require_environment(self._uow, slug, name)
            if name is EnvironmentName.PRODUCTION and change.auto_stop_hours is not None:
                raise ValidationError("auto-stop can only be set on development")
            self._reject_shared_branch(environment, validated)
            before = (environment.branch, environment.auto_deploy, environment.auto_stop_hours)
            previous_backup = environment.include_in_backups
            _apply_update(environment, validated)
            environment.updated_at = self._clock()
            if actor is not None:
                self._audit_update(environment, slug, before, previous_backup, actor)
            return environment

    def _reject_shared_branch(self, environment: Environment, change: EnvironmentUpdate) -> None:
        if change.branch is None:
            return
        for sibling in self._uow.environments.list_for_project(environment.project_id):
            if sibling.id != environment.id and sibling.branch == change.branch:
                raise ValidationError(
                    f"the {sibling.name.value} environment already tracks branch {change.branch}"
                )

    def _audit_update(
        self,
        environment: Environment,
        slug: str,
        before: tuple[str, bool, int | None],
        previous_backup: bool,
        actor: Actor,
    ) -> None:
        details: dict[str, object] = {
            "project": slug,
            "environment": environment.name.value,
            "branch": {"from": before[0], "to": environment.branch},
            "auto_deploy": {"from": before[1], "to": environment.auto_deploy},
            "auto_stop_hours": {"from": before[2], "to": environment.auto_stop_hours},
            "include_in_backups": {"from": previous_backup, "to": environment.include_in_backups},
            "compose": {
                "project": environment.compose_project,
                "files": list(environment.compose_files),
                "env_files": list(environment.compose_env_files),
                "services": list(environment.services),
            },
        }
        target = AuditTarget("environment", str(environment.id))
        add_audit_event(
            self._uow, AuditEntry(actor, "environment.update", target, details), self._clock
        )


def _validate_update(change: EnvironmentUpdate) -> EnvironmentUpdate:
    return EnvironmentUpdate(
        branch=str(BranchName(change.branch)) if change.branch is not None else None,
        auto_deploy=change.auto_deploy,
        auto_stop_hours=change.auto_stop_hours,
        set_auto_stop_hours=change.set_auto_stop_hours,
        compose_project=validate_compose_project(change.compose_project),
        clear_compose_project=change.clear_compose_project,
        compose_files=validate_compose_files(change.compose_files)
        if change.compose_files is not None
        else None,
        compose_env_files=validate_compose_env_files(change.compose_env_files)
        if change.compose_env_files is not None
        else None,
        services=validate_service_names(change.services) if change.services is not None else None,
        include_in_backups=change.include_in_backups,
    )


def _apply_update(environment: Environment, change: EnvironmentUpdate) -> None:
    if change.branch is not None:
        environment.branch = change.branch
    if change.auto_deploy is not None:
        environment.auto_deploy = change.auto_deploy
    if change.set_auto_stop_hours:
        environment.auto_stop_hours = change.auto_stop_hours
    if change.include_in_backups is not None:
        environment.include_in_backups = change.include_in_backups
    if change.clear_compose_project:
        environment.compose_project = None
    elif change.compose_project is not None:
        environment.compose_project = change.compose_project
    if change.compose_files is not None:
        environment.compose_files = change.compose_files
    if change.compose_env_files is not None:
        environment.compose_env_files = change.compose_env_files
    if change.services is not None:
        environment.services = change.services
