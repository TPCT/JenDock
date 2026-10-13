"""Database connection queries."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from shiftcodes.repositories.models import DatabaseConnection


class DatabaseConnectionRepository:
    """Queries for ``DatabaseConnection``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, connection: DatabaseConnection) -> DatabaseConnection:
        """Persist ``connection`` and assign its id."""
        self._session.add(connection)
        self._session.flush()
        return connection

    def get(self, environment_id: int, name: str) -> DatabaseConnection | None:
        """Return the connection ``name`` of an environment or ``None``."""
        query = select(DatabaseConnection).where(
            DatabaseConnection.environment_id == environment_id, DatabaseConnection.name == name
        )
        return self._session.scalar(query)

    def list_for_environment(self, environment_id: int) -> Sequence[DatabaseConnection]:
        """Return the connections of an environment ordered by name."""
        query = select(DatabaseConnection).where(
            DatabaseConnection.environment_id == environment_id
        )
        return self._session.scalars(query.order_by(DatabaseConnection.name)).all()

    def count_for_environment(self, environment_id: int) -> int:
        """Number of connections of an environment."""
        query = select(func.count()).where(DatabaseConnection.environment_id == environment_id)
        return int(self._session.scalar(query.select_from(DatabaseConnection)) or 0)

    def delete(self, connection: DatabaseConnection) -> None:
        """Delete one connection."""
        self._session.delete(connection)
        self._session.flush()
