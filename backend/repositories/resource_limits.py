"""Queries for the resource limits of compose services."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.repositories.models import ResourceLimit


class ResourceLimitRepository:
    """Queries for ``ResourceLimit``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, row: ResourceLimit) -> ResourceLimit:
        """Persist ``row`` and assign its id."""
        self._session.add(row)
        self._session.flush()
        return row

    def get(self, environment_id: int, service: str) -> ResourceLimit | None:
        """The limits of one service of an environment."""
        query = select(ResourceLimit).where(
            ResourceLimit.environment_id == environment_id, ResourceLimit.service == service
        )
        return self._session.scalar(query)

    def list_for_environment(self, environment_id: int) -> Sequence[ResourceLimit]:
        """Every service's limits of an environment, ordered by service."""
        query = select(ResourceLimit).where(ResourceLimit.environment_id == environment_id)
        return self._session.scalars(query.order_by(ResourceLimit.service)).all()

    def delete(self, row: ResourceLimit) -> None:
        """Delete one service's limits."""
        self._session.delete(row)
        self._session.flush()
