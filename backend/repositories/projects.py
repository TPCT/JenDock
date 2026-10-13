"""Project queries."""

from __future__ import annotations

from collections.abc import Collection, Sequence

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from backend.domain.paging import PageQuery
from backend.repositories.models import Project
from backend.repositories.paging import contains, ordered

PROJECT_SORTS = {
    "name": func.lower(Project.name),
    "slug": Project.slug,
    "created": Project.created_at,
}


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

    def search(
        self,
        query: PageQuery,
        *,
        include_archived: bool = False,
        slugs: Collection[str] | None = None,
    ) -> tuple[Sequence[Project], int]:
        """One page of projects and the total number of matches.

        ``slugs`` limits the result to those projects (an empty collection matches none): the
        caller's access, applied before paging and counting. The search text matches the name,
        slug and repository. Sort keys: ``name`` (default, a-z), ``slug`` and ``created``
        (newest first).
        """
        filters: list[ColumnElement[bool]] = []
        if not include_archived:
            filters.append(Project.archived_at.is_(None))
        if slugs is not None:
            filters.append(Project.slug.in_(slugs))
        if query.term is not None:
            filters.append(contains([Project.name, Project.slug, Project.repository], query.term))
        statement = ordered(
            select(Project).where(*filters),
            query,
            PROJECT_SORTS,
            "name",
            descending=frozenset({"created"}),
            tiebreak=Project.slug,
        )
        rows = self._session.scalars(statement.limit(query.limit).offset(query.offset)).all()
        total = self._session.scalar(select(func.count()).select_from(Project).where(*filters))
        return rows, int(total or 0)
