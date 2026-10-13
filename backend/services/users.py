"""Admin user management use cases."""

from __future__ import annotations

from backend.domain.actor import Actor
from backend.domain.audit import AuditEntry, AuditTarget
from backend.domain.clock import Clock, utcnow
from backend.domain.errors import ConflictError, NotFoundError
from backend.domain.values import Username, validate_password
from backend.repositories.database import UnitOfWork
from backend.repositories.models import User
from backend.services.audit import add_audit_event
from backend.services.passwords import PasswordHasher


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
