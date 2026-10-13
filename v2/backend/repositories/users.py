"""User queries."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.repositories.models import User


class UserRepository:
    """Queries for ``User``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, user: User) -> User:
        """Persist ``user`` and assign its id."""
        self._session.add(user)
        self._session.flush()
        return user

    def get(self, user_id: int) -> User | None:
        """Return the user with ``user_id`` or ``None``."""
        return self._session.get(User, user_id)

    def get_by_username(self, username: str) -> User | None:
        """Return the user with ``username`` or ``None``."""
        return self._session.scalar(select(User).where(User.username == username))

    def list(self) -> Sequence[User]:
        """Return all users ordered by username."""
        return self._session.scalars(select(User).order_by(User.username)).all()
