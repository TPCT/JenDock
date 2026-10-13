"""Deployment queries."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shiftcodes.domain.enums import DeploymentStatus
from shiftcodes.repositories.models import Deployment


class DeploymentRepository:
    """Queries for ``Deployment``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, deployment: Deployment) -> Deployment:
        """Persist ``deployment`` and assign its id."""
        self._session.add(deployment)
        self._session.flush()
        return deployment

    def get(self, deployment_id: int) -> Deployment | None:
        """Return the deployment with ``deployment_id`` or ``None``."""
        return self._session.get(Deployment, deployment_id)

    def list_for_environment(
        self, environment_id: int, *, limit: int, offset: int
    ) -> Sequence[Deployment]:
        """Return deployments of an environment, newest first."""
        query = (
            select(Deployment)
            .where(Deployment.environment_id == environment_id)
            .order_by(Deployment.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return self._session.scalars(query).all()

    def count_for_environment(self, environment_id: int) -> int:
        """Return how many deployments an environment has."""
        query = (
            select(func.count())
            .select_from(Deployment)
            .where(Deployment.environment_id == environment_id)
        )
        return self._session.scalar(query) or 0

    def pending_for_run(self, run_id: int) -> Deployment | None:
        """Return the pending (redeploy) row of a run, if one exists."""
        query = (
            select(Deployment)
            .where(Deployment.run_id == run_id, Deployment.status == DeploymentStatus.PENDING)
            .order_by(Deployment.id)
            .limit(1)
        )
        return self._session.scalar(query)

    def latest_started(self, environment_id: int) -> Deployment | None:
        """Return the newest deployment that actually started (not a pending redeploy)."""
        query = (
            select(Deployment)
            .where(
                Deployment.environment_id == environment_id,
                Deployment.status != DeploymentStatus.PENDING,
            )
            .order_by(Deployment.id.desc())
            .limit(1)
        )
        return self._session.scalar(query)

    def list_by_status(self, status: DeploymentStatus) -> Sequence[Deployment]:
        """Return deployments in ``status``, oldest first."""
        query = select(Deployment).where(Deployment.status == status).order_by(Deployment.id)
        return self._session.scalars(query).all()
