"""Pipeline definition queries (append-only history)."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.repositories.models import PipelineDefinition


class PipelineRepository:
    """Queries for ``PipelineDefinition``. There is deliberately no update method."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, definition: PipelineDefinition) -> PipelineDefinition:
        """Persist a new pipeline version."""
        self._session.add(definition)
        self._session.flush()
        return definition

    def latest_version(self, environment_id: int) -> int:
        """Return the highest version number, or 0 when none exists."""
        query = select(func.max(PipelineDefinition.version)).where(
            PipelineDefinition.environment_id == environment_id
        )
        return self._session.scalar(query) or 0

    def get_latest(self, environment_id: int) -> PipelineDefinition | None:
        """Return the active (highest) version or ``None``."""
        query = (
            select(PipelineDefinition)
            .where(PipelineDefinition.environment_id == environment_id)
            .order_by(PipelineDefinition.version.desc())
            .limit(1)
        )
        return self._session.scalar(query)

    def get_version(self, environment_id: int, version: int) -> PipelineDefinition | None:
        """Return one stored version or ``None``."""
        query = select(PipelineDefinition).where(
            PipelineDefinition.environment_id == environment_id,
            PipelineDefinition.version == version,
        )
        return self._session.scalar(query)

    def list_history(self, environment_id: int) -> Sequence[PipelineDefinition]:
        """Return all versions, newest first."""
        query = (
            select(PipelineDefinition)
            .where(PipelineDefinition.environment_id == environment_id)
            .order_by(PipelineDefinition.version.desc())
        )
        return self._session.scalars(query).all()
