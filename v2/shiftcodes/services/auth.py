"""Password login with rate limiting and rehash-on-login."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from shiftcodes.domain.actor import Actor
from shiftcodes.domain.audit import AuditEntry, AuditTarget
from shiftcodes.domain.clock import Clock, utcnow
from shiftcodes.domain.enums import ActorType
from shiftcodes.domain.errors import AuthenticationError
from shiftcodes.repositories.database import UnitOfWork
from shiftcodes.repositories.models import User
from shiftcodes.services.audit import add_audit_event
from shiftcodes.services.passwords import PasswordHasher
from shiftcodes.services.rate_limit import LoginRateLimiter
from shiftcodes.services.sessions import NewSession, SessionService

logger = logging.getLogger(__name__)
INVALID_CREDENTIALS = "invalid username or password"


@dataclass(frozen=True, slots=True)
class LoginResult:
    """Outcome of a successful login."""

    session: NewSession
    user_id: int
    username: str


class AuthService:
    """Verifies credentials and opens sessions."""

    def __init__(
        self,
        uow: UnitOfWork,
        hasher: PasswordHasher,
        limiter: LoginRateLimiter,
        sessions: SessionService,
        clock: Clock = utcnow,
    ) -> None:
        self._uow = uow
        self._hasher = hasher
        self._limiter = limiter
        self._sessions = sessions
        self._clock = clock

    def login(self, username: str, password: str, client_ip: str) -> LoginResult:
        """Return a new session or raise ``AuthenticationError`` / ``RateLimitedError``.

        Unknown users and wrong passwords are indistinguishable: same error, same work.
        """
        name = username.strip().lower()
        self._limiter.check(name, client_ip)
        user = self._verify(name, password)
        if user is None:
            self._limiter.record_failure(name, client_ip)
            raise AuthenticationError(INVALID_CREDENTIALS)
        self._limiter.reset_username(name)
        session = self._sessions.create(user.id)
        self._record(Actor(ActorType.USER, str(user.id)), "auth.login", user)
        return LoginResult(session, user.id, user.username)

    def logout(self, token: str, user_id: int) -> None:
        """End the session and audit it."""
        self._sessions.delete(token)
        with self._uow:
            user = self._uow.users.get(user_id)
            if user is not None:
                self._stage(Actor(ActorType.USER, str(user.id)), "auth.logout", user)

    def _verify(self, name: str, password: str) -> User | None:
        with self._uow:
            user = self._uow.users.get_by_username(name)
            if user is None or not user.is_active:
                self._hasher.verify_dummy(password)
                logger.warning("login failed", extra={"known_user": False})
                return None
            if not self._hasher.verify(user.password_hash, password):
                self._stage(Actor.system("auth"), "auth.login_failed", user)
                logger.warning("login failed", extra={"known_user": True, "user_id": user.id})
                return None
            if self._hasher.needs_rehash(user.password_hash):
                user.password_hash = self._hasher.hash(password)
            return user

    def _record(self, actor: Actor, action: str, user: User) -> None:
        with self._uow:
            self._stage(actor, action, user)

    def _stage(self, actor: Actor, action: str, user: User) -> None:
        target = AuditTarget("user", str(user.id))
        entry = AuditEntry(actor, action, target, {"username": user.username})
        add_audit_event(self._uow, entry, self._clock)
