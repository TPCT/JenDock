"""Secret store use cases. Plaintext leaves only via ``reveal`` and ``resolve_for_run``."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from shiftcodes.domain.actor import Actor
from shiftcodes.domain.audit import AuditEntry, AuditTarget
from shiftcodes.domain.clock import Clock, utcnow
from shiftcodes.domain.enums import EnvironmentName
from shiftcodes.domain.errors import NotFoundError, ValidationError
from shiftcodes.domain.values import SecretName
from shiftcodes.logging import SecretRedactor
from shiftcodes.repositories.database import UnitOfWork
from shiftcodes.repositories.models import Environment, Secret
from shiftcodes.services.audit import add_audit_event
from shiftcodes.services.lookup import require_environment
from shiftcodes.services.secret_cipher import SecretCipher

MAX_SECRET_BYTES = 64 * 1024
WORKER_ACTOR_NAME = "worker"


@dataclass(frozen=True, slots=True)
class SecretInfo:
    """Secret metadata. Deliberately has no value or ciphertext field."""

    name: str
    created_at: datetime
    updated_at: datetime


class SecretService:
    """Per-environment encrypted secret store."""

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

    def set(
        self, slug: str, env: EnvironmentName, name: str, value: str, actor: Actor
    ) -> SecretInfo:
        """Create or rotate a secret; audits the name only."""
        secret_name = str(SecretName(name))
        _validate_value(value)
        ciphertext = self._cipher.encrypt(value)
        self._register(value)
        with self._uow:
            environment = require_environment(self._uow, slug, env)
            secret = self._uow.secrets.get(environment.id, secret_name)
            created = secret is None
            secret = self._store(environment, secret, secret_name, ciphertext)
            action = "secret.create" if created else "secret.rotate"
            self._audit(actor, action, environment, {"name": secret_name})
            return _info(secret)

    def _store(
        self, environment: Environment, secret: Secret | None, name: str, ciphertext: bytes
    ) -> Secret:
        now = self._clock()
        if secret is None:
            new = Secret(
                environment_id=environment.id,
                name=name,
                ciphertext=ciphertext,
                created_at=now,
                updated_at=now,
            )
            return self._uow.secrets.add(new)
        secret.ciphertext = ciphertext
        secret.updated_at = now
        return secret

    def list_names(self, slug: str, env: EnvironmentName) -> Sequence[SecretInfo]:
        """Return secret metadata (never values) ordered by name."""
        with self._uow:
            environment = require_environment(self._uow, slug, env)
            return [_info(s) for s in self._uow.secrets.list_for_environment(environment.id)]

    def delete(self, slug: str, env: EnvironmentName, name: str, actor: Actor) -> None:
        """Delete a secret and audit it."""
        secret_name = str(SecretName(name))
        with self._uow:
            environment = require_environment(self._uow, slug, env)
            secret = self._uow.secrets.get(environment.id, secret_name)
            if secret is None:
                raise NotFoundError(f"secret not found: {secret_name}")
            self._uow.secrets.delete(secret)
            self._audit(actor, "secret.delete", environment, {"name": secret_name})

    def reveal(self, slug: str, env: EnvironmentName, name: str, actor: Actor) -> str:
        """Return the plaintext of one secret and write an audit event (without the value)."""
        secret_name = str(SecretName(name))
        with self._uow:
            environment = require_environment(self._uow, slug, env)
            secret = self._uow.secrets.get(environment.id, secret_name)
            if secret is None:
                raise NotFoundError(f"secret not found: {secret_name}")
            value = self._cipher.decrypt(secret.ciphertext)
            self._audit(actor, "secret.reveal", environment, {"name": secret_name})
        self._register(value)
        return value

    def resolve_for_run(
        self, environment_id: int, names: Sequence[str], run_id: int | None = None
    ) -> dict[str, str]:
        """Return plaintext values for ``names`` for the worker; audited as system actor."""
        wanted = sorted({str(SecretName(name)) for name in names})
        with self._uow:
            environment = self._uow.environments.get(environment_id)
            if environment is None:
                raise NotFoundError(f"environment not found: {environment_id}")
            found = {s.name: s for s in self._uow.secrets.get_many(environment_id, wanted)}
            missing = [name for name in wanted if name not in found]
            if missing:
                raise NotFoundError(f"secrets not found: {', '.join(missing)}")
            values = {name: self._cipher.decrypt(found[name].ciphertext) for name in wanted}
            details: dict[str, object] = {"names": wanted, "run_id": run_id}
            self._audit(Actor.system(WORKER_ACTOR_NAME), "secret.resolve", environment, details)
        for value in values.values():
            self._register(value)
        return values

    def _audit(
        self, actor: Actor, action: str, environment: Environment, details: dict[str, object]
    ) -> None:
        target = AuditTarget("environment", str(environment.id))
        add_audit_event(self._uow, AuditEntry(actor, action, target, details), self._clock)

    def _register(self, value: str) -> None:
        if self._redactor is not None:
            self._redactor.register(value)


def _validate_value(value: str) -> None:
    if not value:
        raise ValidationError("secret value must not be empty")
    if len(value.encode()) > MAX_SECRET_BYTES:
        raise ValidationError(f"secret value exceeds {MAX_SECRET_BYTES} bytes")


def _info(secret: Secret) -> SecretInfo:
    return SecretInfo(secret.name, secret.created_at, secret.updated_at)
