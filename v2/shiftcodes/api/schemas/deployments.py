"""Deployment schemas: history entries and the result of a redeploy."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from shiftcodes.domain.enums import DeploymentStatus, PipelineSource, RunStatus, RunTrigger
from shiftcodes.services.deployments import DeploymentPage, DeploymentView, RedeployResult


class ServiceImageResponse(BaseModel):
    """One service of a deployment: what ran before and what it was rolled to."""

    service: str
    previous_image: str | None
    previous_image_id: str | None
    new_image: str
    new_image_id: str | None


class DeploymentResponse(BaseModel):
    """One deployment of an environment."""

    id: int
    run_id: int
    run_number: int
    status: DeploymentStatus
    commit_sha: str
    services: list[ServiceImageResponse]
    message: str | None
    redeploy_of: int | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def from_view(cls, view: DeploymentView) -> DeploymentResponse:
        """Build from the history read model."""
        return cls(
            id=view.id,
            run_id=view.run_id,
            run_number=view.run_number,
            status=view.status,
            commit_sha=view.commit_sha,
            services=[
                ServiceImageResponse(
                    service=c.service,
                    previous_image=c.previous_image,
                    previous_image_id=c.previous_image_id,
                    new_image=c.new_image,
                    new_image_id=c.new_image_id,
                )
                for c in view.services
            ],
            message=view.message,
            redeploy_of=view.redeploy_of_id,
            created_at=view.created_at,
            started_at=view.started_at,
            finished_at=view.finished_at,
        )


class DeploymentPageResponse(BaseModel):
    """A page of the deployment history."""

    items: list[DeploymentResponse]
    total: int
    limit: int
    offset: int

    @classmethod
    def from_page(cls, page: DeploymentPage, limit: int, offset: int) -> DeploymentPageResponse:
        """Build from a page of the read model."""
        return cls(
            items=[DeploymentResponse.from_view(item) for item in page.items],
            total=page.total,
            limit=limit,
            offset=offset,
        )


class RedeployRunResponse(BaseModel):
    """The queued run (the ``RunSummary`` shape of the API contract)."""

    id: int
    number: int
    project: str
    environment: str
    trigger: RunTrigger
    branch: str
    commit_sha: str
    pipeline_source: PipelineSource
    status: RunStatus
    message: str | None
    cancel_requested_at: datetime | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class RedeployResponse(BaseModel):
    """Result of ``POST .../deployments/{id}/redeploy``."""

    run: RedeployRunResponse
    deployment_id: int

    @classmethod
    def from_result(cls, result: RedeployResult) -> RedeployResponse:
        """Build from the queued run."""
        run = result.run
        return cls(
            run=RedeployRunResponse(
                id=run.id,
                number=run.number,
                project=result.project,
                environment=result.environment,
                trigger=run.trigger,
                branch=run.branch,
                commit_sha=run.commit_sha,
                pipeline_source=run.pipeline_source,
                status=run.status,
                message=run.message,
                cancel_requested_at=run.cancel_requested_at,
                created_at=run.created_at,
                started_at=run.started_at,
                finished_at=run.finished_at,
            ),
            deployment_id=result.deployment_id,
        )
