"""User management and access grant schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.domain.access import AccessRole, ContainerPermission, GlobalRole
from backend.domain.enums import EnvironmentName
from backend.repositories.models import User
from backend.services.access import GrantRecord, ProjectAccess
from backend.services.users import CreatedUser


class UserSummary(BaseModel):
    """A user as admins see them (never a password hash)."""

    id: int
    username: str
    display_name: str
    email: str | None
    role: GlobalRole
    is_active: bool
    must_change_password: bool
    created_at: datetime

    @classmethod
    def from_user(cls, user: User) -> UserSummary:
        """Build from the stored user."""
        return cls(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            email=user.email,
            role=user.role,
            is_active=user.is_active,
            must_change_password=user.must_change_password,
            created_at=user.created_at,
        )


class CreateUserRequest(BaseModel):
    """Body of ``POST /api/users``: the password is generated and shown once."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    display_name: str = Field(default="", max_length=120)
    email: str | None = Field(default=None, max_length=254)
    role: GlobalRole = GlobalRole.MEMBER


class UpdateUserRequest(BaseModel):
    """Body of ``PATCH /api/users/{id}``; absent fields stay as they are."""

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=254)
    role: GlobalRole | None = None
    is_active: bool | None = None


class OneTimePasswordResponse(BaseModel):
    """A user with the generated one-time password (this response is the only place it appears;
    the user must change it at the next sign-in)."""

    user: UserSummary
    one_time_password: str = Field(repr=False)

    @classmethod
    def from_created(cls, created: CreatedUser) -> OneTimePasswordResponse:
        """Build from the service result."""
        if created.one_time_password is None:
            raise RuntimeError("no one-time password was generated")
        return cls(
            user=UserSummary.from_user(created.user), one_time_password=created.one_time_password
        )


class SetGrantRequest(BaseModel):
    """Body of ``PUT /api/projects/{slug}/access``: add or change one grant.

    No ``environment``: the whole project. ``environment`` only: that environment. With
    ``service`` too: container permissions (``permissions``) on that compose service.
    """

    model_config = ConfigDict(extra="forbid")

    user_id: int
    environment: EnvironmentName | None = None
    service: str | None = Field(default=None, min_length=1, max_length=64)
    role: AccessRole | None = None
    permissions: list[ContainerPermission] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def _shape(self) -> SetGrantRequest:
        if self.service is not None and self.environment is None:
            raise ValueError("a container grant needs an environment")
        return self


class GrantResponse(BaseModel):
    """One grant."""

    id: int
    user_id: int
    username: str
    display_name: str
    project: str
    environment: EnvironmentName | None
    service: str | None
    role: AccessRole | None
    permissions: list[ContainerPermission]
    created_at: datetime

    @classmethod
    def from_record(cls, record: GrantRecord) -> GrantResponse:
        """Build from the service record."""
        return cls(
            id=record.id,
            user_id=record.user_id,
            username=record.username,
            display_name=record.display_name,
            project=record.project,
            environment=record.environment,
            service=record.service,
            role=record.role,
            permissions=list(record.permissions),
            created_at=record.created_at,
        )


class ProjectAccessResponse(BaseModel):
    """Who has access to a project: owners and admins implicitly, members by grant."""

    privileged: list[UserSummary]
    grants: list[GrantResponse]

    @classmethod
    def from_access(cls, access: ProjectAccess) -> ProjectAccessResponse:
        """Build from the service result."""
        return cls(
            privileged=[UserSummary.from_user(u) for u in access.privileged],
            grants=[GrantResponse.from_record(g) for g in access.grants],
        )
