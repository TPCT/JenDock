"""Login session queries."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import delete, or_, select
from sqlalchemy.orm import Session as DbSession

from backend.repositories.models import Session


class SessionRepository:
    """Queries for ``Session``."""

    def __init__(self, session: DbSession) -> None:
        self._session = session

    def add(self, login_session: Session) -> Session:
        """Persist ``login_session`` and assign its id."""
        self._session.add(login_session)
        self._session.flush()
        return login_session

    def get_by_token_hash(self, token_hash: str) -> Session | None:
        """Return the session with ``token_hash`` or ``None``."""
        return self._session.scalar(select(Session).where(Session.token_hash == token_hash))

    def list_for_user(self, user_id: int) -> Sequence[Session]:
        """Return every session of a user."""
        return self._session.scalars(select(Session).where(Session.user_id == user_id)).all()

    def list_live_for_user(
        self, user_id: int, now: datetime, idle_cutoff: datetime
    ) -> list[Session]:
        """Return the live sessions of a user, most recently seen first."""
        query = (
            select(Session)
            .where(
                Session.user_id == user_id,
                Session.expires_at > now,
                Session.last_seen_at > idle_cutoff,
            )
            .order_by(Session.last_seen_at.desc(), Session.id.desc())
        )
        return list(self._session.scalars(query).all())

    def get_for_user(self, user_id: int, session_id: int) -> Session | None:
        """Return session ``session_id`` if it belongs to ``user_id``."""
        query = select(Session).where(Session.id == session_id, Session.user_id == user_id)
        return self._session.scalar(query)

    def delete_others(self, user_id: int, keep_session_id: int) -> int:
        """Delete every session of a user except ``keep_session_id``; return how many."""
        result = self._session.execute(
            delete(Session).where(Session.user_id == user_id, Session.id != keep_session_id)
        )
        return int(getattr(result, "rowcount", 0) or 0)

    def delete_for_user(self, user_id: int) -> int:
        """Delete every session of a user; return how many."""
        result = self._session.execute(delete(Session).where(Session.user_id == user_id))
        return int(getattr(result, "rowcount", 0) or 0)

    def delete(self, login_session: Session) -> None:
        """Delete one session."""
        self._session.delete(login_session)
        self._session.flush()

    def delete_stale(self, now: datetime, idle_cutoff: datetime) -> int:
        """Delete sessions past their absolute expiry or idle since ``idle_cutoff``."""
        stale = or_(Session.expires_at <= now, Session.last_seen_at <= idle_cutoff)
        result = self._session.execute(delete(Session).where(stale))
        return int(getattr(result, "rowcount", 0) or 0)
