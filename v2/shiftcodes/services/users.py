"""Admin user management use cases."""

from __future__ import annotations

from shiftcodes.domain.actor import Actor
from shiftcodes.domain.audit import AuditEntry, AuditTarget
from shiftcodes.domain.clock import Clock, utcnow
from shiftcodes.domain.errors import ConflictError, NotFoundError
from shiftcodes.domain.values import Username, validate_password
from shiftcodes.repositories.database import UnitOfWork
from shiftcodes.repositories.models import User
from shiftcodes.services.audit import add_audit_event
from shiftcodes.services.passwords import PasswordHasher


class UserService:
    """Creates users and resets their passwords; every change is audited."""

    def __init__(self, uow: UnitOfWork, hasher: PasswordHasher, clock: Clock = utcnow) -> None:
        self._uow = uow
        self._hasher = hasher
        self._clock = clock

    def create(self, username: str, password: str, actor: Actor) -> User:
        """Create an active user; ``ConflictError`` if the name is taken."""
        name = str(Username(username))
        password_hash = self._hasher.hash(validate_password(password))
        with self._uow:
            if self._uow.users.get_by_username(name) is not None:
                raise ConflictError(f"user already exists: {name}")
            user = User(username=name, password_hash=password_hash, created_at=self._clock())
            self._uow.users.add(user)
            self._audit(actor, "user.create", user)
            return user

    def reset_password(self, username: str, password: str, actor: Actor) -> None:
        """Set a new password and end all of the user's sessions."""
        password_hash = self._hasher.hash(validate_password(password))
        with self._uow:
            user = self._uow.users.get_by_username(username)
            if user is None:
                raise NotFoundError(f"user not found: {username}")
            user.password_hash = password_hash
            self._uow.sessions.delete_for_user(user.id)
            self._audit(actor, "user.reset_password", user)

    def _audit(self, actor: Actor, action: str, user: User) -> None:
        target = AuditTarget("user", str(user.id))
        entry = AuditEntry(actor, action, target, {"username": user.username})
        add_audit_event(self._uow, entry, self._clock)
