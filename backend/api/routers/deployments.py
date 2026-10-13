"""Deployment history of an environment and redeploy of an earlier version."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from backend.api.deps import Container, require
from backend.api.paging import Paging
from backend.api.principal import Principal
from backend.api.schemas.deployments import DeploymentPageResponse, RedeployResponse
from backend.domain.access import Action
from backend.domain.enums import DeploymentStatus, EnvironmentName

router = APIRouter(
    prefix="/api/projects/{slug}/environments/{env}/deployments", tags=["deployments"]
)
Reader = Annotated[Principal, Depends(require(Action.DEPLOYMENT_READ))]
Deployer = Annotated[Principal, Depends(require(Action.DEPLOYMENT_REDEPLOY))]


@router.get("")
def list_deployments(
    slug: str,
    env: EnvironmentName,
    _principal: Reader,
    container: Container,
    paging: Paging,
    deployment_status: Annotated[DeploymentStatus | None, Query(alias="status")] = None,
) -> DeploymentPageResponse:
    """Deployments of the environment (newest first), searchable by commit, message or run.

    ``sort`` is ``created`` or ``finished``.
    """
    page = container.deployment_service().history(slug, env, paging.query, deployment_status)
    return DeploymentPageResponse.from_page(page)


@router.post("/{deployment_id}/redeploy", status_code=status.HTTP_201_CREATED)
def redeploy(
    slug: str, env: EnvironmentName, deployment_id: int, principal: Deployer, container: Container
) -> RedeployResponse:
    """Queue a run that deploys the images of an earlier successful deployment again."""
    result = container.deployment_service().redeploy(slug, env, deployment_id, principal.actor)
    return RedeployResponse.from_result(result)
