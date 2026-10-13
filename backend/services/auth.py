"""Password login (with an optional second factor), rate limiting and rehash-on-login."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from backend.domain.access import GlobalRole
from backend.domain.actor import Actor
from backend.domain.audit import AuditDetails, AuditEntry, AuditTarget
from backend.domain.clock import Clock, utcnow
from backend.domain.enums import ActorType
from backend.domain.errors import AuthenticationError
from backend.repositories.database import UnitOfWork
from backend.repositories.models import User
from backend.services.audit import add_audit_event
from backend.services.mfa_challenges import MfaChallengeStore
from backend.services.passwords import PasswordHasher
from backend.services.rate_limit import LoginRateLimiter
from backend.services.sessions import NewSession, SessionService
from backend.services.two_factor import TwoFactorService

logger = logging.getLogger(__name__)
INVALID_CREDENTIALS = "invalid username or password"
INVALID_SECOND_FACTOR = "invalid or expired verification code"


@dataclass(frozen=True, slots=True)
class LoginResult:
    """Outcome of a login step: a session, or (two-factor users) a challenge to complete.

    Exactly one of ``session`` and ``challenge`` is set.
    """

    user_id: int
    username: str
    role: GlobalRole
    must_change_password: bool
    session: NewSession | None = None
    challenge: str | None = None


class AuthService:
    """Verifies credentials and opens sessions."""

    def __init__(  # noqa: PLR0913, PLR0917 - collaborators of one use case
        self,
        uow: UnitOfWork,
        hasher: PasswordHasher,
        limiter: LoginRateLimiter,
        sessions: SessionService,
        clock: Clock = utcnow,
        two_factor: TwoFactorService | None = None,
        challenges: MfaChallengeStore | None = None,
    ) -> None:
        self._uow = uow
        self._hasher = hasher
        self._limiter = limiter
        self._sessions = sessions
        self._clock = clock
        self._two_factor = two_factor
        self._challenges = challenges or MfaChallengeStore(clock)

    def login(
        self, username: str, password: str, client_ip: str, user_agent: str | None = None
    ) -> LoginResult:
        """Return a new session (or a second-step challenge) or raise
        ``AuthenticationError`` / ``RateLimitedError``.

        Unknown users and wrong passwords are indistinguishable: same error, same work.
        """
        name = username.strip().lower()
        self._limiter.check(name, client_ip)
        user = self._verify(name, password)
        if user is None:
            self._limiter.record_failure(name, client_ip)
            raise AuthenticationError(INVALID_CREDENTIALS)
        if self._two_factor is not None and self._two_factor.is_enabled(user.id):
            # The password is right but no session yet; failures are not reset until step two.
            return LoginResult(
                user.id,
                user.username,
                user.role,
                user.must_change_password,
                challenge=self._challenges.issue(user.id),
            )
        self._limiter.reset_username(name)
        return self._open_session(user, client_ip, user_agent, {})

    def complete_login(
        self, challenge: str, code: str, client_ip: str, user_agent: str | None = None
    ) -> LoginResult:
        """Second step: a TOTP or recovery code opens the session of a pending login."""
        user_id = self._challenges.peek(challenge)
        user = self._active_user(user_id) if user_id is not None else None
        if user is None or self._two_factor is None:
            raise AuthenticationError(INVALID_SECOND_FACTOR)
        self._limiter.check(user.username, client_ip)
        method = self._two_factor.verify_login(user.id, code)
        if method is None:
            self._limiter.record_failure(user.username, client_ip)
            self._challenges.fail(challenge)
            self._record(Actor.system("auth"), "auth.two_factor_failed", user, {})
            raise AuthenticationError(INVALID_SECOND_FACTOR)
        self._challenges.consume(challenge)
        self._limiter.reset_username(user.username)
        if method != "totp":
            self._record(Actor(ActorType.USER, str(user.id)), "auth.recovery_code_used", user, {})
        return self._open_session(user, client_ip, user_agent, {"two_factor": method})

    def logout(self, token: str, user_id: int) -> None:
        """End the session and audit it."""
        self._sessions.delete(token)
        with self._uow:
            user = self._uow.users.get(user_id)
            if user is not None:
                self._stage(Actor(ActorType.USER, str(user.id)), "auth.logout", user)

    def _open_session(
        self, user: User, client_ip: str, user_agent: str | None, details: AuditDetails
    ) -> LoginResult:
        session = self._sessions.create(user.id, client_ip, user_agent)
        self._record(Actor(ActorType.USER, str(user.id)), "auth.login", user, details)
        return LoginResult(
            user.id, user.username, user.role, user.must_change_password, session=session
        )

    def _active_user(self, user_id: int) -> User | None:
        with self._uow:
            user = self._uow.users.get(user_id)
            return user if user is not None and user.is_active else None

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

    def _record(self, actor: Actor, action: str, user: User, details: AuditDetails) -> None:
        with self._uow:
            self._stage(actor, action, user, details)

    def _stage(
        self, actor: Actor, action: str, user: User, details: AuditDetails | None = None
    ) -> None:
        target = AuditTarget("user", str(user.id))
        entry = AuditEntry(actor, action, target, {"username": user.username, **(details or {})})
        add_audit_event(self._uow, entry, self._clock)
