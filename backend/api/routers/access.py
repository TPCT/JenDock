"""Project access: who may do what on a project, an environment or a container service."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from backend.api.deps import Container, Target, require
from backend.api.principal import Principal
from backend.api.schemas.users import GrantResponse, ProjectAccessResponse, SetGrantRequest
from backend.domain.access import Action

router = APIRouter(prefix="/api/projects/{slug}/access", tags=["access"])
AccessManager = Annotated[Principal, Depends(require(Action.ACCESS_MANAGE, Target.PROJECT))]


@router.get("")
def project_access(
    slug: str, _principal: AccessManager, container: Container
) -> ProjectAccessResponse:
    """Owners and admins (full access) and the members' grants."""
    return ProjectAccessResponse.from_access(container.access_service().for_project(slug))


@router.put("")
def set_grant(
    slug: str, body: SetGrantRequest, principal: AccessManager, container: Container
) -> GrantResponse:
    """Add a grant or change the role of the existing one for the same target."""
    record = container.access_service().set_grant(
        slug,
        body.user_id,
        principal.actor,
        environment=body.environment,
        service=body.service,
        role=body.role,
        permissions=body.permissions,
        created_by=principal.user_id,
    )
    return GrantResponse.from_record(record)


@router.delete("/{grant_id}", status_code=204)
def remove_grant(
    slug: str, grant_id: int, principal: AccessManager, container: Container
) -> Response:
    """Remove one grant."""
    container.access_service().remove_grant(slug, grant_id, principal.actor)
    return Response(status_code=204)
