"""Access grants: who may do what on a project, an environment or one container service."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from backend.domain.access import AccessRole, ContainerPermission, GlobalRole, Grant
from backend.domain.actor import Actor
from backend.domain.audit import AuditEntry, AuditTarget
from backend.domain.clock import Clock, utcnow
from backend.domain.enums import EnvironmentName
from backend.domain.errors import NotFoundError, ValidationError
from backend.domain.values import validate_service_names
from backend.repositories.database import UnitOfWork
from backend.repositories.models import AccessGrant, Project, User
from backend.services.audit import add_audit_event
from backend.services.lookup import require_project


@dataclass(frozen=True, slots=True)
class GrantRecord:
    """A stored grant with its user and project names."""

    id: int
    user_id: int
    username: str
    display_name: str
    role_of_user: GlobalRole
    project: str
    environment: EnvironmentName | None
    service: str | None
    role: AccessRole | None
    permissions: tuple[ContainerPermission, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ProjectAccess:
    """Everyone with access to a project: explicit grants plus the implicit admins."""

    grants: tuple[GrantRecord, ...]
    privileged: tuple[User, ...]


def scope_key(environment: EnvironmentName | None, service: str | None) -> str:
    """The unique key of a grant target within one user and project."""
    if environment is None:
        return "project"
    if service is None:
        return f"env:{environment.value}"
    return f"svc:{environment.value}:{service}"


def _permissions(row: AccessGrant) -> tuple[ContainerPermission, ...]:
    return tuple(sorted((ContainerPermission(p) for p in row.permissions), key=str))


def _record(row: AccessGrant, user: User, slug: str) -> GrantRecord:
    return GrantRecord(
        id=row.id,
        user_id=user.id,
        username=user.username,
        display_name=user.display_name,
        role_of_user=user.role,
        project=slug,
        environment=row.environment,
        service=row.service,
        role=row.role,
        permissions=_permissions(row),
        created_at=row.created_at,
    )


class AccessService:
    """Loads a user's grants for the policy and manages them (audited)."""

    def __init__(self, uow: UnitOfWork, clock: Clock = utcnow) -> None:
        self._uow = uow
        self._clock = clock

    def load(self, user_id: int) -> tuple[Grant, ...]:
        """The grants the policy evaluates for a user."""
        with self._uow:
            return tuple(
                Grant(
                    project.slug,
                    row.environment,
                    row.service,
                    row.role,
                    frozenset(_permissions(row)),
                )
                for row, project in self._uow.grants.list_for_user(user_id)
            )

    def for_project(self, slug: str) -> ProjectAccess:
        """Grants of a project and the owners and admins who have access implicitly."""
        with self._uow:
            project = require_project(self._uow, slug)
            records = tuple(
                _record(row, user, project.slug)
                for row, user in self._uow.grants.list_for_project(project.id)
            )
            privileged = tuple(u for u in self._uow.users.list() if u.role.is_privileged)
            return ProjectAccess(records, privileged)

    def for_user(self, user_id: int) -> tuple[GrantRecord, ...]:
        """Every grant of a user on active projects."""
        with self._uow:
            user = self._uow.users.get(user_id)
            if user is None:
                raise NotFoundError(f"user not found: {user_id}")
            return tuple(
                _record(row, user, project.slug)
                for row, project in self._uow.grants.list_for_user(user_id)
            )

    def set_grant(  # noqa: PLR0913 - one value per grant field
        self,
        slug: str,
        user_id: int,
        actor: Actor,
        *,
        environment: EnvironmentName | None = None,
        service: str | None = None,
        role: AccessRole | None = None,
        permissions: Collection[ContainerPermission] = (),
        created_by: int | None = None,
    ) -> GrantRecord:
        """Add the grant or replace the one for the same target (the role is changed)."""
        permission_set = frozenset(permissions)
        self._validate(environment, service, role, permission_set)
        with self._uow:
            project = require_project(self._uow, slug)
            user = self._uow.users.get(user_id)
            if user is None:
                raise NotFoundError(f"user not found: {user_id}")
            if user.role.is_privileged:
                raise ValidationError("owners and admins already have access to everything")
            key = scope_key(environment, service)
            row = self._uow.grants.find(user.id, project.id, key)
            action = "access.grant_update" if row else "access.grant_add"
            if row is None:
                row = AccessGrant(
                    user_id=user.id,
                    project_id=project.id,
                    environment=environment,
                    service=service,
                    scope_key=key,
                    created_by=created_by,
                    created_at=self._clock(),
                )
                self._uow.grants.add(row)
            row.role = role
            row.permissions = sorted(p.value for p in permission_set)
            self._audit(actor, action, project, user, row)
            return _record(row, user, project.slug)

    def remove_grant(self, slug: str, grant_id: int, actor: Actor) -> None:
        """Remove one grant of the project."""
        with self._uow:
            project = require_project(self._uow, slug)
            row = self._uow.grants.get(grant_id)
            if row is None or row.project_id != project.id:
                raise NotFoundError(f"grant not found: {grant_id}")
            user = self._uow.users.get(row.user_id)
            if user is None:
                raise NotFoundError(f"user not found: {row.user_id}")
            self._audit(actor, "access.grant_remove", project, user, row)
            self._uow.grants.delete(row)

    @staticmethod
    def _validate(
        environment: EnvironmentName | None,
        service: str | None,
        role: AccessRole | None,
        permissions: frozenset[ContainerPermission],
    ) -> None:
        if service is not None:
            if environment is None:
                raise ValidationError("a container grant needs an environment")
            validate_service_names([service])
            if role is not None or not permissions:
                raise ValidationError("a container grant carries permissions, not a role")
        elif role is None or permissions:
            raise ValidationError("a project or environment grant carries a role")

    def _audit(
        self, actor: Actor, action: str, project: Project, user: User, row: AccessGrant
    ) -> None:
        details: dict[str, object] = {
            "project": project.slug,
            "user": user.username,
            "user_id": user.id,
            "environment": row.environment.value if row.environment else None,
            "service": row.service,
            "role": row.role.value if row.role else None,
            "permissions": list(row.permissions),
        }
        entry = AuditEntry(actor, action, AuditTarget("project", project.slug), details)
        add_audit_event(self._uow, entry, self._clock)
