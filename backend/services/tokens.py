"""API tokens: scoped bearer credentials for agents and the MCP server."""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from backend.domain.access import GlobalRole
from backend.domain.actor import Actor
from backend.domain.audit import AuditEntry, AuditTarget
from backend.domain.clock import Clock, utcnow
from backend.domain.enums import TokenScope
from backend.domain.errors import NotFoundError, ValidationError
from backend.domain.values import validate_display_name
from backend.repositories.database import UnitOfWork
from backend.repositories.models import ApiToken, User
from backend.services.audit import add_audit_event
from backend.services.sessions import hash_token

TOKEN_PREFIX = "jdt_"  # noqa: S105 - public format marker, not a credential
TOKEN_BYTES = 32
DISPLAY_PREFIX_LENGTH = 8
MAX_TOKEN_LENGTH = 128
LAST_USED_RESOLUTION = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class TokenInfo:
    """Token metadata. Deliberately has no token or hash field."""

    id: int
    name: str
    prefix: str
    scopes: tuple[TokenScope, ...]
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True, slots=True)
class CreatedToken:
    """A new token; ``value`` is shown to the user exactly once."""

    info: TokenInfo
    value: str


@dataclass(frozen=True, slots=True)
class AuthenticatedToken:
    """The identity behind a valid bearer token."""

    id: int
    name: str
    scopes: frozenset[TokenScope]
    owner_id: int
    owner_role: GlobalRole


class TokenService:
    """Creates, lists, revokes and authenticates API tokens."""

    def __init__(self, uow: UnitOfWork, clock: Clock = utcnow) -> None:
        self._uow = uow
        self._clock = clock

    def create(
        self,
        name: str,
        scopes: Sequence[TokenScope],
        created_by: int,
        expires_at: datetime | None,
        actor: Actor,
    ) -> CreatedToken:
        """Create a token owned by user ``created_by``; only its hash is stored."""
        display_name = validate_display_name(name)
        unique_scopes = sorted(set(scopes), key=lambda scope: scope.value)
        if not unique_scopes:
            raise ValidationError("a token needs at least one scope")
        now = self._clock()
        if expires_at is not None and expires_at <= now:
            raise ValidationError("expires_at must be in the future")
        value = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
        with self._uow:
            row = ApiToken(
                name=display_name,
                token_prefix=value[:DISPLAY_PREFIX_LENGTH],
                token_hash=hash_token(value),
                scopes=[scope.value for scope in unique_scopes],
                created_by=created_by,
                created_at=now,
                expires_at=expires_at,
            )
            self._uow.tokens.add(row)
            details: dict[str, object] = {
                "name": display_name,
                "scopes": [scope.value for scope in unique_scopes],
                "prefix": row.token_prefix,
            }
            self._audit(actor, "token.create", row, details)
            return CreatedToken(_info(row), value)

    def list(self, owner_id: int | None = None) -> Sequence[TokenInfo]:
        """Return tokens (never their values), newest first; only ``owner_id``'s when given."""
        with self._uow:
            rows = self._uow.tokens.list()
            return [_info(row) for row in rows if owner_id in (None, row.created_by)]

    def revoke(self, token_id: int, actor: Actor, owner_id: int | None = None) -> TokenInfo:
        """Revoke a token; revoking twice keeps the first revocation time.

        With ``owner_id`` only that user's tokens are found (other tokens do not exist for them).
        """
        with self._uow:
            row = self._uow.tokens.get(token_id)
            if row is None or owner_id not in (None, row.created_by):
                raise NotFoundError(f"token not found: {token_id}")
            if row.revoked_at is None:
                row.revoked_at = self._clock()
                self._audit(actor, "token.revoke", row, {"name": row.name})
            return _info(row)

    def authenticate(self, value: str) -> AuthenticatedToken | None:
        """Return the identity for a bearer token, or ``None`` if invalid, revoked or expired.

        ``last_used_at`` is written at most once per minute.
        """
        if not value.startswith(TOKEN_PREFIX) or len(value) > MAX_TOKEN_LENGTH:
            return None
        now = self._clock()
        with self._uow:
            row = self._uow.tokens.get_by_hash(hash_token(value))
            if row is None or row.revoked_at is not None:
                return None
            if row.expires_at is not None and row.expires_at <= now:
                return None
            owner = self._uow.users.get(row.created_by)
            if owner is None or not owner.is_active:
                return None  # a token never outlives its owner's access
            if row.last_used_at is None or now - row.last_used_at >= LAST_USED_RESOLUTION:
                row.last_used_at = now
            return _authenticated(row, owner)

    def current(self, token_id: int) -> AuthenticatedToken | None:
        """Re-check a token by id (no secret needed): ``None`` once revoked or expired."""
        now = self._clock()
        with self._uow:
            row = self._uow.tokens.get(token_id)
            if row is None or row.revoked_at is not None:
                return None
            if row.expires_at is not None and row.expires_at <= now:
                return None
            owner = self._uow.users.get(row.created_by)
            if owner is None or not owner.is_active:
                return None
            return _authenticated(row, owner)

    def _audit(self, actor: Actor, action: str, row: ApiToken, details: dict[str, object]) -> None:
        target = AuditTarget("api_token", str(row.id))
        add_audit_event(self._uow, AuditEntry(actor, action, target, details), self._clock)


def _authenticated(row: ApiToken, owner: User) -> AuthenticatedToken:
    return AuthenticatedToken(row.id, row.name, frozenset(_scopes(row)), owner.id, owner.role)


def _scopes(row: ApiToken) -> list[TokenScope]:
    return [TokenScope(value) for value in row.scopes]


def _info(row: ApiToken) -> TokenInfo:
    return TokenInfo(
        id=row.id,
        name=row.name,
        prefix=row.token_prefix,
        scopes=tuple(_scopes(row)),
        created_at=row.created_at,
        last_used_at=row.last_used_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
    )
