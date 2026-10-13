"""Deployment history of an environment and redeploy of an earlier version."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from shiftcodes.api.deps import Container, require_scope
from shiftcodes.api.principal import Principal
from shiftcodes.api.schemas.deployments import DeploymentPageResponse, RedeployResponse
from shiftcodes.domain.enums import EnvironmentName, TokenScope
from shiftcodes.services.deployments import MAX_PAGE_SIZE

router = APIRouter(
    prefix="/api/projects/{slug}/environments/{env}/deployments", tags=["deployments"]
)
Reader = Annotated[Principal, Depends(require_scope(TokenScope.READ))]
Deployer = Annotated[Principal, Depends(require_scope(TokenScope.DEPLOY))]


@router.get("")
def list_deployments(
    slug: str,
    env: EnvironmentName,
    _principal: Reader,
    container: Container,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> DeploymentPageResponse:
    """Deployments of the environment, newest first."""
    page = container.deployment_service().history(slug, env, limit=limit, offset=offset)
    return DeploymentPageResponse.from_page(page, limit, offset)


@router.post("/{deployment_id}/redeploy", status_code=status.HTTP_201_CREATED)
def redeploy(
    slug: str, env: EnvironmentName, deployment_id: int, principal: Deployer, container: Container
) -> RedeployResponse:
    """Queue a run that deploys the images of an earlier successful deployment again."""
    result = container.deployment_service().redeploy(slug, env, deployment_id, principal.actor)
    return RedeployResponse.from_result(result)
