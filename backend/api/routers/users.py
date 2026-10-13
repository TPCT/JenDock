"""Members: create, list, edit, disable, delete and reset the password of users.

Owners and admins only, browser sessions only (a token never manages users). Admins cannot
create, change, disable, delete or reset owners; nobody changes their own role or disables or
deletes themselves; the last owner is protected. Every change is audited.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from backend.api.deps import Container, Target, require
from backend.api.principal import Principal
from backend.api.schemas.users import (
    CreateUserRequest,
    GrantResponse,
    OneTimePasswordResponse,
    UpdateUserRequest,
    UserSummary,
)
from backend.domain.access import Action
from backend.services.users import ActingUser, UserChange

router = APIRouter(prefix="/api/users", tags=["users"])
Manager = Annotated[Principal, Depends(require(Action.USER_MANAGE, Target.NONE))]


def _acting(principal: Principal) -> ActingUser:
    return ActingUser(principal.user_id, principal.role)


@router.get("")
def list_users(_principal: Manager, container: Container) -> list[UserSummary]:
    """Every user ordered by username."""
    return [UserSummary.from_user(user) for user in container.user_service().list()]


@router.post("", status_code=201)
def create_user(
    body: CreateUserRequest, principal: Manager, container: Container
) -> OneTimePasswordResponse:
    """Create a user with a generated one-time password (returned once; changed at first login)."""
    created = container.user_service().create_with_generated_password(
        body.username,
        principal.actor,
        role=body.role,
        display_name=body.display_name,
        email=body.email,
        by=_acting(principal),
    )
    return OneTimePasswordResponse.from_created(created)


@router.get("/{user_id}")
def get_user(user_id: int, _principal: Manager, container: Container) -> UserSummary:
    """One user."""
    return UserSummary.from_user(container.user_service().get(user_id))


@router.patch("/{user_id}")
def update_user(
    user_id: int, body: UpdateUserRequest, principal: Manager, container: Container
) -> UserSummary:
    """Change the profile, the global role or enable/disable the account."""
    change = UserChange(
        display_name=body.display_name,
        email=body.email,
        role=body.role,
        is_active=body.is_active,
    )
    user = container.user_service().update(user_id, change, principal.actor, _acting(principal))
    return UserSummary.from_user(user)


@router.delete("/{user_id}", status_code=204)
def delete_user(user_id: int, principal: Manager, container: Container) -> Response:
    """Delete a user with their sessions, tokens and grants."""
    container.user_service().delete(user_id, principal.actor, _acting(principal))
    return Response(status_code=204)


@router.post("/{user_id}/reset-password")
def reset_password(
    user_id: int, principal: Manager, container: Container
) -> OneTimePasswordResponse:
    """Replace the password by a generated one-time password (shown once, must be changed)."""
    created = container.user_service().admin_reset_password(
        user_id, principal.actor, _acting(principal)
    )
    return OneTimePasswordResponse.from_created(created)


@router.get("/{user_id}/access")
def user_access(user_id: int, _principal: Manager, container: Container) -> list[GrantResponse]:
    """Every grant of a user across projects."""
    records = container.access_service().for_user(user_id)
    return [GrantResponse.from_record(record) for record in records]
