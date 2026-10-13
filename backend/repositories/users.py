"""User queries."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from backend.domain.access import GlobalRole
from backend.repositories.models import RecoveryCode, User


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

    def count_active_owners(self) -> int:
        """Return how many enabled users hold the ``owner`` role."""
        query = (
            select(func.count())
            .select_from(User)
            .where(User.role == GlobalRole.OWNER, User.is_active.is_(True))
        )
        return self._session.scalar(query) or 0

    def delete(self, user: User) -> None:
        """Delete ``user``; sessions, tokens and grants go with it (ON DELETE CASCADE)."""
        self._session.delete(user)
        self._session.flush()

    def get_many(self, user_ids: Sequence[int]) -> Sequence[User]:
        """Return the users with the given ids (one query)."""
        if not user_ids:
            return []
        return self._session.scalars(select(User).where(User.id.in_(user_ids))).all()

    def advance_totp_step(self, user_id: int, step: int) -> bool:
        """Record ``step`` as the last accepted time step unless it is not newer.

        Compare-and-set: of two requests presenting the same code only one gets ``True``.
        """
        stale = or_(User.totp_last_step.is_(None), User.totp_last_step < step)
        result = self._session.execute(
            update(User).where(User.id == user_id, stale).values(totp_last_step=step)
        )
        return int(getattr(result, "rowcount", 0) or 0) == 1

    def count_recovery_codes(self, user_id: int) -> int:
        """How many unused recovery codes the user has."""
        query = (
            select(func.count())
            .select_from(RecoveryCode)
            .where(RecoveryCode.user_id == user_id, RecoveryCode.used_at.is_(None))
        )
        return self._session.scalar(query) or 0

    def replace_recovery_codes(self, user_id: int, hashes: Sequence[str], now: datetime) -> None:
        """Delete all recovery codes of the user and store ``hashes`` as the new set."""
        self._session.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user_id))
        for code_hash in hashes:
            self._session.add(RecoveryCode(user_id=user_id, code_hash=code_hash, created_at=now))
        self._session.flush()

    def consume_recovery_code(self, user_id: int, code_hash: str, now: datetime) -> bool:
        """Mark an unused code as used; ``True`` only for the first use."""
        result = self._session.execute(
            update(RecoveryCode)
            .where(
                RecoveryCode.user_id == user_id,
                RecoveryCode.code_hash == code_hash,
                RecoveryCode.used_at.is_(None),
            )
            .values(used_at=now)
        )
        return int(getattr(result, "rowcount", 0) or 0) == 1

    def delete_recovery_codes(self, user_id: int) -> None:
        """Remove every recovery code of the user."""
        self._session.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user_id))
