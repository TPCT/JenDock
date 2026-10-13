"""Encrypted platform-level secrets (today: the Cloudflare API token).

Runs in the worker, the only process that holds the secret key. The plaintext never leaves
through an API response, an audit record or a log line: it is registered with the redactor
and audits carry names only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from backend.domain.actor import Actor
from backend.domain.audit import AuditEntry, AuditTarget
from backend.domain.clock import Clock, utcnow
from backend.domain.errors import ValidationError
from backend.logging import SecretRedactor
from backend.repositories.database import UnitOfWork
from backend.repositories.models import PlatformSecret
from backend.services.audit import add_audit_event
from backend.services.secret_cipher import SecretCipher

CLOUDFLARE_TOKEN = "cloudflare_api_token"  # noqa: S105 - the stored entry's name, not a value
MAX_TOKEN_LENGTH = 512


@dataclass(frozen=True, slots=True)
class PlatformSecretInfo:
    """Whether a platform secret is stored, and when it last changed. Never the value."""

    name: str
    configured: bool
    updated_at: datetime | None


def secret_info(uow: UnitOfWork, name: str) -> PlatformSecretInfo:
    """Metadata only (the ciphertext is not even loaded); usable from the API process."""
    with uow:
        row = uow.platform.secret_metadata(name)
    if row is None:
        return PlatformSecretInfo(name, False, None)
    updated = row[1]
    return PlatformSecretInfo(name, True, updated if isinstance(updated, datetime) else None)


class PlatformSecretService:
    """Store, read and delete platform secrets."""

    def __init__(
        self,
        uow: UnitOfWork,
        cipher: SecretCipher,
        redactor: SecretRedactor | None = None,
        clock: Clock = utcnow,
    ) -> None:
        self._uow = uow
        self._cipher = cipher
        self._redactor = redactor
        self._clock = clock

    def set(self, name: str, value: str, actor: Actor) -> PlatformSecretInfo:
        """Create or replace; audits the name only."""
        cleaned = value.strip()
        if not cleaned or len(cleaned) > MAX_TOKEN_LENGTH or any(c.isspace() for c in cleaned):
            raise ValidationError("the token must be a single value of at most 512 characters")
        if self._redactor is not None:
            self._redactor.register(cleaned)
        ciphertext = self._cipher.encrypt(cleaned)
        now = self._clock()
        with self._uow:
            existing = self._uow.platform.get_secret(name)
            if existing is None:
                self._uow.platform.add_secret(
                    PlatformSecret(name=name, ciphertext=ciphertext, created_at=now, updated_at=now)
                )
            else:
                existing.ciphertext = ciphertext
                existing.updated_at = now
            self._audit(actor, "integration.credential_set", name)
        return PlatformSecretInfo(name, True, now)

    def clear(self, name: str, actor: Actor) -> None:
        """Delete the secret if present; audited."""
        with self._uow:
            existing = self._uow.platform.get_secret(name)
            if existing is not None:
                self._uow.platform.delete_secret(existing)
                self._audit(actor, "integration.credential_clear", name)

    def resolve(self, name: str) -> str | None:
        """The plaintext, or ``None`` when not stored. For the worker's own use only."""
        with self._uow:
            row = self._uow.platform.get_secret(name)
            ciphertext = row.ciphertext if row is not None else None
        if ciphertext is None:
            return None
        value = self._cipher.decrypt(ciphertext)
        if self._redactor is not None:
            self._redactor.register(value)
        return value

    def _audit(self, actor: Actor, action: str, name: str) -> None:
        entry = AuditEntry(actor, action, AuditTarget("platform", name), {"name": name})
        add_audit_event(self._uow, entry, self._clock)
