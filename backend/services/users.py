"""User management use cases: create, edit, disable, delete and password resets.

Every change is audited. Rules that protect the platform live here: the last active owner can
never be demoted, disabled or deleted, nobody changes their own role or deletes or disables
themselves, and only owners manage owners (``policy.require_user_management``).
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Sequence
from dataclasses import dataclass

from backend.domain.access import GlobalRole
from backend.domain.actor import Actor
from backend.domain.audit import AuditEntry, AuditTarget
from backend.domain.clock import Clock, utcnow
from backend.domain.errors import ConflictError, NotFoundError, ValidationError
from backend.domain.values import MAX_NAME_LENGTH, Username, validate_password
from backend.repositories.database import UnitOfWork
from backend.repositories.models import User
from backend.services.audit import add_audit_event
from backend.services.passwords import PasswordHasher
from backend.services.policy import require_user_management

GENERATED_PASSWORD_BYTES = 15
MAX_EMAIL_LENGTH = 254
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


@dataclass(frozen=True, slots=True)
class ActingUser:
    """The user performing a management action (``None`` means the platform itself, e.g. CLI)."""

    user_id: int
    role: GlobalRole


@dataclass(frozen=True, slots=True)
class UserChange:
    """Fields to change; ``None`` leaves a field alone (``email=""`` clears it)."""

    display_name: str | None = None
    email: str | None = None
    role: GlobalRole | None = None
    is_active: bool | None = None


@dataclass(frozen=True, slots=True)
class CreatedUser:
    """A new user and, when one was generated, its one-time password (shown once)."""

    user: User
    one_time_password: str | None


def generate_password() -> str:
    """A random one-time password that satisfies the length policy."""
    return secrets.token_urlsafe(GENERATED_PASSWORD_BYTES)


def _clean_display_name(value: str) -> str:
    name = value.strip()
    if len(name) > MAX_NAME_LENGTH:
        raise ValidationError(f"display name must be at most {MAX_NAME_LENGTH} characters")
    return name


def _clean_email(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    email = value.strip()
    if len(email) > MAX_EMAIL_LENGTH or not _EMAIL.fullmatch(email):
        raise ValidationError("invalid email address")
    return email


class WrongPasswordError(ValidationError):
    """The current password given to a password change is not correct."""


class UserService:
    """Creates, edits and removes users; every change is audited."""

    def __init__(self, uow: UnitOfWork, hasher: PasswordHasher, clock: Clock = utcnow) -> None:
        self._uow = uow
        self._hasher = hasher
        self._clock = clock

    def create(  # noqa: PLR0913 - one value per user field
        self,
        username: str,
        password: str,
        actor: Actor,
        *,
        role: GlobalRole = GlobalRole.OWNER,
        display_name: str = "",
        email: str | None = None,
        must_change_password: bool = False,
        by: ActingUser | None = None,
    ) -> User:
        """Create an active user; ``ConflictError`` if the name is taken.

        The default ``owner`` role is for the CLI (the first admin); the API always passes one.
        """
        name = str(Username(username))
        password_hash = self._hasher.hash(validate_password(password))
        if by is not None:
            require_user_management(by.role, None, role)
        with self._uow:
            if self._uow.users.get_by_username(name) is not None:
                raise ConflictError(f"user already exists: {name}")
            user = User(
                username=name,
                password_hash=password_hash,
                created_at=self._clock(),
                role=role,
                display_name=_clean_display_name(display_name),
                email=_clean_email(email),
                must_change_password=must_change_password,
            )
            self._uow.users.add(user)
            self._audit(actor, "user.create", user, {"role": role.value})
            return user

    def create_with_generated_password(  # noqa: PLR0913 - one value per user field
        self,
        username: str,
        actor: Actor,
        *,
        role: GlobalRole,
        display_name: str = "",
        email: str | None = None,
        by: ActingUser | None = None,
    ) -> CreatedUser:
        """Create a user with a random one-time password that must be changed at first sign-in."""
        password = generate_password()
        user = self.create(
            username,
            password,
            actor,
            role=role,
            display_name=display_name,
            email=email,
            must_change_password=True,
            by=by,
        )
        return CreatedUser(user, password)

    def list(self) -> Sequence[User]:
        """All users ordered by username."""
        with self._uow:
            return list(self._uow.users.list())

    def get(self, user_id: int) -> User:
        """One user; ``NotFoundError`` when absent."""
        with self._uow:
            return self._require(user_id)

    def update(
        self, user_id: int, change: UserChange, actor: Actor, by: ActingUser | None = None
    ) -> User:
        """Edit profile, role or enabled state."""
        with self._uow:
            user = self._require(user_id)
            if by is not None:
                require_user_management(by.role, user.role, change.role)
                if by.user_id == user.id and (
                    change.role not in (None, user.role) or change.is_active is False
                ):
                    raise ConflictError("you cannot change your own role or disable yourself")
            details = self._apply(user, change)
            if details:
                self._audit(actor, "user.update", user, details)
            return user

    def delete(self, user_id: int, actor: Actor, by: ActingUser | None = None) -> None:
        """Delete a user with their sessions, tokens and grants (history keeps no reference)."""
        with self._uow:
            user = self._require(user_id)
            if by is not None:
                require_user_management(by.role, user.role)
                if by.user_id == user.id:
                    raise ConflictError("you cannot delete yourself")
            self._guard_last_owner(user)
            self._audit(actor, "user.delete", user, {"role": user.role.value})
            self._uow.users.delete(user)

    def reset_password(self, username: str, password: str, actor: Actor) -> None:
        """Set a new password and end all of the user's sessions (CLI)."""
        password_hash = self._hasher.hash(validate_password(password))
        with self._uow:
            user = self._uow.users.get_by_username(username)
            if user is None:
                raise NotFoundError(f"user not found: {username}")
            user.password_hash = password_hash
            self._uow.sessions.delete_for_user(user.id)
            self._audit(actor, "user.reset_password", user, {})

    def admin_reset_password(
        self, user_id: int, actor: Actor, by: ActingUser | None = None
    ) -> CreatedUser:
        """Give a user a generated one-time password they must change at the next sign-in.

        The password is returned once and never stored or audited in clear; the user's
        sessions end now.
        """
        password = generate_password()
        password_hash = self._hasher.hash(password)
        with self._uow:
            user = self._require(user_id)
            if by is not None:
                require_user_management(by.role, user.role)
            user.password_hash = password_hash
            user.must_change_password = True
            self._uow.sessions.delete_for_user(user.id)
            self._audit(actor, "user.password_reset", user, {"must_change": True})
            return CreatedUser(user, password)

    def change_password(
        self, user_id: int, current: str, new: str, actor: Actor, keep_session_id: int | None
    ) -> None:
        """The user's own change: needs the current password; other sessions end."""
        validate_password(new)
        with self._uow:
            user = self._require(user_id)
            if not self._hasher.verify(user.password_hash, current):
                raise WrongPasswordError("the current password is not correct")
            if current == new:
                raise ValidationError("the new password must differ from the current one")
            user.password_hash = self._hasher.hash(new)
            user.must_change_password = False
            for row in self._uow.sessions.list_for_user(user.id):
                if row.id != keep_session_id:
                    self._uow.sessions.delete(row)
            self._audit(actor, "user.password_change", user, {})

    def _apply(self, user: User, change: UserChange) -> dict[str, object]:
        details: dict[str, object] = {}
        if change.display_name is not None:
            user.display_name = _clean_display_name(change.display_name)
            details["display_name"] = user.display_name
        if change.email is not None:
            user.email = _clean_email(change.email)
            details["email"] = user.email
        if change.role is not None and change.role is not user.role:
            self._guard_last_owner(user)
            details["role"] = {"from": user.role.value, "to": change.role.value}
            user.role = change.role
        if change.is_active is not None and change.is_active != user.is_active:
            if not change.is_active:
                self._guard_last_owner(user)
                self._uow.sessions.delete_for_user(user.id)
            user.is_active = change.is_active
            details["is_active"] = change.is_active
        return details

    def _guard_last_owner(self, user: User) -> None:
        if (
            user.role is GlobalRole.OWNER
            and user.is_active
            and self._uow.users.count_active_owners() <= 1
        ):
            raise ConflictError("the last owner cannot be demoted, disabled or deleted")

    def _require(self, user_id: int) -> User:
        user = self._uow.users.get(user_id)
        if user is None:
            raise NotFoundError(f"user not found: {user_id}")
        return user

    def _audit(self, actor: Actor, action: str, user: User, details: dict[str, object]) -> None:
        target = AuditTarget("user", str(user.id))
        entry = AuditEntry(actor, action, target, {"username": user.username, **details})
        add_audit_event(self._uow, entry, self._clock)
