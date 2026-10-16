"""Deployment queries."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from backend.domain.enums import DeploymentStatus
from backend.domain.paging import PageQuery
from backend.repositories.models import Deployment, Run
from backend.repositories.paging import contains, ordered

DEPLOYMENT_SORTS = {"created": Deployment.id, "finished": Deployment.finished_at}


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

    def search_for_environment(
        self, environment_id: int, query: PageQuery, status: DeploymentStatus | None = None
    ) -> tuple[Sequence[Deployment], int]:
        """One page of an environment's deployments and the total number of matches.

        The search text matches the commit, the message and the run number (``42`` or ``#42``).
        Sort keys: ``created`` (default, newest first) and ``finished``.
        """
        filters: list[ColumnElement[bool]] = [Deployment.environment_id == environment_id]
        if status is not None:
            filters.append(Deployment.status == status)
        term = query.term
        if term is not None:
            matches = contains([Deployment.commit_sha, Deployment.message], term)
            digits = term.removeprefix("#")
            if digits.isdecimal() and len(digits) <= 9:
                run_ids = select(Run.id).where(Run.number == int(digits))
                matches = matches | Deployment.run_id.in_(run_ids)
            filters.append(matches)
        statement = ordered(
            select(Deployment).where(*filters),
            query,
            DEPLOYMENT_SORTS,
            "created",
            descending=frozenset({"created", "finished"}),
            tiebreak=Deployment.id,
        )
        rows = self._session.scalars(statement.limit(query.limit).offset(query.offset)).all()
        total = self._session.scalar(select(func.count()).select_from(Deployment).where(*filters))
        return rows, int(total or 0)

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

    def latest_successful(self, environment_id: int) -> Deployment | None:
        """Newest successfully completed deployment of an environment."""
        query = (
            select(Deployment)
            .where(
                Deployment.environment_id == environment_id,
                Deployment.status == DeploymentStatus.SUCCEEDED,
                Deployment.finished_at.is_not(None),
            )
            .order_by(Deployment.finished_at.desc())
            .limit(1)
        )
        return self._session.scalar(query)

    def list_by_status(self, status: DeploymentStatus) -> Sequence[Deployment]:
        """Return deployments in ``status``, oldest first."""
        query = select(Deployment).where(Deployment.status == status).order_by(Deployment.id)
        return self._session.scalars(query).all()
