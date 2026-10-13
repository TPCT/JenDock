"""Project queries."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shiftcodes.repositories.models import Project


class ProjectRepository:
    """Queries for ``Project``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, project: Project) -> Project:
        """Persist ``project`` and assign its id."""
        self._session.add(project)
        self._session.flush()
        return project

    def get(self, project_id: int) -> Project | None:
        """Return the project with ``project_id`` or ``None``."""
        return self._session.get(Project, project_id)

    def get_by_slug(self, slug: str) -> Project | None:
        """Return the project with ``slug`` or ``None``."""
        return self._session.scalar(select(Project).where(Project.slug == slug))

    def list_by_repository(self, repository: str) -> Sequence[Project]:
        """Return the active projects of a repository (GitHub names are case-insensitive)."""
        query = (
            select(Project)
            .where(func.lower(Project.repository) == repository.lower())
            .where(Project.archived_at.is_(None))
            .order_by(Project.slug)
        )
        return self._session.scalars(query).all()

    def list(self, *, include_archived: bool = False) -> Sequence[Project]:
        """Return projects ordered by slug, excluding archived ones by default."""
        query = select(Project).order_by(Project.slug)
        if not include_archived:
            query = query.where(Project.archived_at.is_(None))
        return self._session.scalars(query).all()
