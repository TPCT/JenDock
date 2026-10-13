"""Access grant queries."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.repositories.models import AccessGrant, Project, User


class AccessGrantRepository:
    """Queries for ``AccessGrant``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, grant: AccessGrant) -> AccessGrant:
        """Persist ``grant`` and assign its id."""
        self._session.add(grant)
        self._session.flush()
        return grant

    def get(self, grant_id: int) -> AccessGrant | None:
        """Return the grant with ``grant_id`` or ``None``."""
        return self._session.get(AccessGrant, grant_id)

    def find(self, user_id: int, project_id: int, scope_key: str) -> AccessGrant | None:
        """Return the grant of a user for one target of a project."""
        query = select(AccessGrant).where(
            AccessGrant.user_id == user_id,
            AccessGrant.project_id == project_id,
            AccessGrant.scope_key == scope_key,
        )
        return self._session.scalar(query)

    def delete(self, grant: AccessGrant) -> None:
        """Delete one grant."""
        self._session.delete(grant)
        self._session.flush()

    def list_for_project(self, project_id: int) -> Sequence[tuple[AccessGrant, User]]:
        """Grants of a project with their users, ordered by user name then scope."""
        query = (
            select(AccessGrant, User)
            .join(User, User.id == AccessGrant.user_id)
            .where(AccessGrant.project_id == project_id)
            .order_by(User.username, AccessGrant.scope_key)
        )
        return [(grant, user) for grant, user in self._session.execute(query).all()]

    def list_for_user(self, user_id: int) -> Sequence[tuple[AccessGrant, Project]]:
        """Grants of a user on active projects, with the project."""
        query = (
            select(AccessGrant, Project)
            .join(Project, Project.id == AccessGrant.project_id)
            .where(AccessGrant.user_id == user_id, Project.archived_at.is_(None))
            .order_by(Project.slug, AccessGrant.scope_key)
        )
        return [(grant, project) for grant, project in self._session.execute(query).all()]
