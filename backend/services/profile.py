"""The signed-in user's own account: profile, password and sessions.

Authorization here is "the caller is this user" (a browser session), enforced by the API layer
by passing the session's user id; nothing in this module looks at roles.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from backend.config import SESSION_IDLE_TIMEOUT
from backend.domain.actor import Actor
from backend.domain.audit import AuditEntry, AuditTarget
from backend.domain.clock import Clock, utcnow
from backend.domain.errors import NotFoundError, ValidationError
from backend.domain.useragent import summarize_user_agent
from backend.repositories.database import UnitOfWork
from backend.repositories.models import User
from backend.services.audit import add_audit_event
from backend.services.users import clean_display_name, clean_email


@dataclass(frozen=True, slots=True)
class ProfileView:
    """The account as the profile page shows it."""

    id: int
    username: str
    display_name: str
    email: str | None
    created_at: datetime
    two_factor_enabled: bool


@dataclass(frozen=True, slots=True)
class SessionView:
    """One live login session of the user."""

    id: int
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    ip_address: str | None
    user_agent: str | None
    client: str
    current: bool


class ProfileService:
    """Reads and changes the signed-in user's own account."""

    def __init__(self, uow: UnitOfWork, clock: Clock = utcnow) -> None:
        self._uow = uow
        self._clock = clock

    def get(self, user_id: int) -> ProfileView:
        """The user's profile."""
        with self._uow:
            return _view(self._require(user_id))

    def update(
        self, user_id: int, display_name: str | None, email: str | None, actor: Actor
    ) -> ProfileView:
        """Set the display name and email (both optional); audited with the changed fields."""
        name = clean_display_name(display_name or "")
        address = clean_email(email)
        with self._uow:
            user = self._require(user_id)
            changed = [
                field
                for field, old, new in (
                    ("display_name", user.display_name, name),
                    ("email", user.email, address),
                )
                if old != new
            ]
            user.display_name = name
            user.email = address
            if changed:
                self._audit(actor, "user.profile_update", user, {"changed": changed})
            return _view(user)

    def list_sessions(self, user_id: int, current_session_id: int) -> list[SessionView]:
        """The user's live sessions, most recently seen first."""
        now = self._clock()
        with self._uow:
            rows = self._uow.sessions.list_live_for_user(user_id, now, now - SESSION_IDLE_TIMEOUT)
            return [
                SessionView(
                    id=row.id,
                    created_at=row.created_at,
                    last_seen_at=row.last_seen_at,
                    expires_at=row.expires_at,
                    ip_address=row.ip_address,
                    user_agent=row.user_agent,
                    client=summarize_user_agent(row.user_agent),
                    current=row.id == current_session_id,
                )
                for row in rows
            ]

    def revoke_session(
        self, user_id: int, session_id: int, current_session_id: int, actor: Actor
    ) -> None:
        """End one of the user's other sessions (the current one ends with logout)."""
        if session_id == current_session_id:
            raise ValidationError("this is your current session; sign out instead")
        with self._uow:
            row = self._uow.sessions.get_for_user(user_id, session_id)
            if row is None:
                raise NotFoundError(f"session not found: {session_id}")
            user = self._require(user_id)
            self._uow.sessions.delete(row)
            self._audit(actor, "session.revoke", user, {"session": session_id})

    def revoke_other_sessions(self, user_id: int, current_session_id: int, actor: Actor) -> int:
        """End every session but the current one; return how many were ended."""
        with self._uow:
            user = self._require(user_id)
            ended = self._uow.sessions.delete_others(user_id, current_session_id)
            if ended:
                self._audit(actor, "session.revoke_others", user, {"sessions_ended": ended})
            return ended

    def _require(self, user_id: int) -> User:
        user = self._uow.users.get(user_id)
        if user is None:
            raise NotFoundError(f"user not found: {user_id}")
        return user

    def _audit(self, actor: Actor, action: str, user: User, extra: dict[str, object]) -> None:
        target = AuditTarget("user", str(user.id))
        entry = AuditEntry(actor, action, target, {"username": user.username, **extra})
        add_audit_event(self._uow, entry, self._clock)


def _view(user: User) -> ProfileView:
    return ProfileView(
        id=user.id,
        username=user.username,
        display_name=user.display_name,
        email=user.email,
        created_at=user.created_at,
        two_factor_enabled=user.totp_enabled_at is not None,
    )
