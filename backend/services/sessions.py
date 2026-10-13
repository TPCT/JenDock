"""Server-side login sessions.

The cookie carries a random token; only its SHA-256 hash is stored. The CSRF token is
an HMAC keyed with the session token, so it is a pure function of the cookie value:
nothing CSRF-related is stored, and a database leak reveals neither token.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime

from backend.config import SESSION_ABSOLUTE_TIMEOUT, SESSION_IDLE_TIMEOUT
from backend.domain.access import GlobalRole
from backend.domain.clock import Clock, utcnow
from backend.repositories.database import UnitOfWork
from backend.repositories.models import Session

TOKEN_BYTES = 32
_CSRF_CONTEXT = b"jendock-csrf-v1"


def hash_token(token: str) -> str:
    """Return the hex SHA-256 of a high-entropy bearer token."""
    return hashlib.sha256(token.encode()).hexdigest()


def derive_csrf_token(session_token: str) -> str:
    """Return the CSRF token belonging to ``session_token``."""
    return hmac.new(session_token.encode(), _CSRF_CONTEXT, hashlib.sha256).hexdigest()


def csrf_matches(session_token: str, supplied: str) -> bool:
    """Compare a supplied CSRF token with the expected one in constant time."""
    expected = derive_csrf_token(session_token)
    return hmac.compare_digest(expected.encode(), supplied.encode())


@dataclass(frozen=True, slots=True)
class NewSession:
    """A freshly created session; ``token`` goes into the cookie and is never stored."""

    token: str
    csrf_token: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class SessionContext:
    """An authenticated session and its user."""

    session_id: int
    user_id: int
    username: str
    csrf_token: str
    expires_at: datetime
    role: GlobalRole
    must_change_password: bool = False
    display_name: str = ""
    email: str | None = None


class SessionService:
    """Creates, resolves and deletes sessions."""

    def __init__(self, uow: UnitOfWork, clock: Clock = utcnow) -> None:
        self._uow = uow
        self._clock = clock

    def create(self, user_id: int) -> NewSession:
        """Start a session for ``user_id`` and purge stale sessions."""
        token = secrets.token_urlsafe(TOKEN_BYTES)
        now = self._clock()
        expires_at = now + SESSION_ABSOLUTE_TIMEOUT
        with self._uow:
            self._uow.sessions.delete_stale(now, now - SESSION_IDLE_TIMEOUT)
            row = Session(
                user_id=user_id,
                token_hash=hash_token(token),
                created_at=now,
                last_seen_at=now,
                expires_at=expires_at,
            )
            self._uow.sessions.add(row)
        return NewSession(token, derive_csrf_token(token), expires_at)

    def resolve(self, token: str, *, touch: bool = True) -> SessionContext | None:
        """Return the live session for ``token`` or ``None``.

        Expired sessions (idle or absolute) are deleted. ``touch=False`` is for background
        requests (event streams) and leaves the idle timer untouched.
        """
        now = self._clock()
        with self._uow:
            row = self._uow.sessions.get_by_token_hash(hash_token(token))
            if row is None:
                return None
            user = self._uow.users.get(row.user_id)
            if user is None or not user.is_active or self._is_expired(row, now):
                self._uow.sessions.delete(row)
                return None
            if touch:
                row.last_seen_at = now
            return SessionContext(
                row.id,
                user.id,
                user.username,
                derive_csrf_token(token),
                row.expires_at,
                role=user.role,
                must_change_password=user.must_change_password,
                display_name=user.display_name,
                email=user.email,
            )

    def delete(self, token: str) -> None:
        """End the session for ``token`` if it exists."""
        with self._uow:
            row = self._uow.sessions.get_by_token_hash(hash_token(token))
            if row is not None:
                self._uow.sessions.delete(row)

    @staticmethod
    def _is_expired(row: Session, now: datetime) -> bool:
        return row.expires_at <= now or row.last_seen_at + SESSION_IDLE_TIMEOUT <= now
