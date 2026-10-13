"""Environment queries."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from shiftcodes.domain.enums import EnvironmentName
from shiftcodes.repositories.models import Environment, Project


class EnvironmentRepository:
    """Queries for ``Environment``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, environment: Environment) -> Environment:
        """Persist ``environment`` and assign its id."""
        self._session.add(environment)
        self._session.flush()
        return environment

    def get(self, environment_id: int) -> Environment | None:
        """Return the environment with ``environment_id`` or ``None``."""
        return self._session.get(Environment, environment_id)

    def get_by_project_slug(self, slug: str, name: EnvironmentName) -> Environment | None:
        """Return the environment ``name`` of the project with ``slug``."""
        query = (
            select(Environment)
            .join(Project, Project.id == Environment.project_id)
            .where(Project.slug == slug, Environment.name == name)
        )
        return self._session.scalar(query)

    def list_for_project(self, project_id: int) -> Sequence[Environment]:
        """Return the environments of a project ordered by name."""
        query = select(Environment).where(Environment.project_id == project_id)
        return self._session.scalars(query.order_by(Environment.name)).all()

    def list_compose_projects(self) -> Sequence[str]:
        """Distinct compose project names of every configured environment."""
        query = select(Environment.compose_project).where(Environment.compose_project.is_not(None))
        return sorted({name for name in self._session.scalars(query) if name})
