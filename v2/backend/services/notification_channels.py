"""Channel configuration use cases. They run in the worker, the only process with the key.

A channel's secret part (webhook URL, signing secret, SMTP host and credentials) is validated,
serialised to JSON and encrypted into ``config_ciphertext``. What leaves this module is the
row without that column plus ``hint`` and ``summary``; audit details name fields, never values.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping

from backend.adapters.notify_senders import Deliverer, DeliveryError
from backend.domain.actor import Actor
from backend.domain.audit import AuditEntry, AuditTarget
from backend.domain.clock import Clock, utcnow
from backend.domain.errors import ConfigurationError, ConflictError, NotFoundError
from backend.domain.notifications import (
    ChannelConfig,
    ChannelKind,
    DeliveryStatus,
    EventPayload,
    NotificationEvent,
    validate_channel_config,
    validate_channel_name,
)
from backend.repositories.database import UnitOfWork
from backend.repositories.notification_models import NotificationChannel, NotificationDelivery
from backend.repositories.notifications import NotificationRepository
from backend.services.audit import add_audit_event
from backend.services.secret_cipher import SecretCipher

logger = logging.getLogger(__name__)
MAX_CHANNELS = 50
TEST_KEY = "test"


def _repo(uow: UnitOfWork) -> NotificationRepository:
    return NotificationRepository(uow.session)


def decrypt_config(cipher: SecretCipher, channel: NotificationChannel) -> dict[str, object]:
    """The channel's secret configuration (worker only)."""
    loaded = json.loads(cipher.decrypt(channel.config_ciphertext))
    if not isinstance(loaded, dict):
        raise ConfigurationError("a channel configuration is corrupt")
    return loaded


def sample_payload() -> EventPayload:
    """The message of "Send test"."""
    return EventPayload(
        event=NotificationEvent.RUN_FIXED,
        dedupe_key=TEST_KEY,
        title="Test notification from Jendock",
        message="If you can read this, the channel works.",
        link_path="/settings/notifications",
    )


class ChannelService:
    """Create, change, delete and test notification channels."""

    def __init__(
        self,
        uow: UnitOfWork,
        cipher: SecretCipher,
        deliverer: Deliverer,
        clock: Clock = utcnow,
    ) -> None:
        self._uow = uow
        self._cipher = cipher
        self._deliverer = deliverer
        self._clock = clock

    def create(
        self, name: str, kind: ChannelKind, config: Mapping[str, object], actor: Actor
    ) -> NotificationChannel:
        """Validate and store a channel; audited."""
        clean_name = validate_channel_name(name)
        validated = validate_channel_config(kind, config)
        now = self._clock()
        with self._uow:
            repo = _repo(self._uow)
            if repo.find_channel(clean_name) is not None:
                raise ConflictError(f"a channel called {clean_name} already exists")
            if len(repo.list_channels()) >= MAX_CHANNELS:
                raise ConflictError(f"at most {MAX_CHANNELS} channels")
            channel = repo.add_channel(
                NotificationChannel(
                    name=clean_name,
                    kind=kind.value,
                    enabled=True,
                    config_ciphertext=self._seal(validated),
                    hint=validated.hint,
                    summary=validated.summary,
                    created_at=now,
                    updated_at=now,
                )
            )
            self._audit(actor, "notification.channel.create", channel, {"kind": kind.value})
            return channel

    def update(
        self,
        channel_id: int,
        actor: Actor,
        *,
        name: str | None = None,
        enabled: bool | None = None,
        config: Mapping[str, object] | None = None,
    ) -> NotificationChannel:
        """Change the name, the switch and/or (part of) the configuration; audited."""
        changed: list[str] = []
        with self._uow:
            repo = _repo(self._uow)
            channel = repo.get_channel_with_config(channel_id)
            if channel is None:
                raise NotFoundError(f"notification channel not found: {channel_id}")
            if name is not None:
                clean = validate_channel_name(name)
                other = repo.find_channel(clean)
                if other is not None and other.id != channel.id:
                    raise ConflictError(f"a channel called {clean} already exists")
                if clean != channel.name:
                    channel.name = clean
                    changed.append("name")
            if enabled is not None and enabled != channel.enabled:
                channel.enabled = enabled
                changed.append("enabled")
            if config:
                current = decrypt_config(self._cipher, channel)
                validated = validate_channel_config(ChannelKind(channel.kind), config, current)
                channel.config_ciphertext = self._seal(validated)
                channel.hint = validated.hint
                channel.summary = validated.summary
                changed.extend(sorted(config))
            channel.updated_at = self._clock()
            self._audit(actor, "notification.channel.update", channel, {"changed": changed})
            return channel

    def delete(self, channel_id: int, actor: Actor) -> None:
        """Remove a channel with its rules and history; audited."""
        with self._uow:
            repo = _repo(self._uow)
            channel = repo.get_channel(channel_id)
            if channel is None:
                raise NotFoundError(f"notification channel not found: {channel_id}")
            self._audit(actor, "notification.channel.delete", channel, {"name": channel.name})
            repo.delete_channel(channel)

    def test(self, channel_id: int, actor: Actor) -> tuple[bool, str | None]:
        """Send a test message now and record it in the history; audited. Never raises for a
        failed delivery: it returns ``(False, reason)``."""
        now = self._clock()
        with self._uow:
            repo = _repo(self._uow)
            channel = repo.get_channel_with_config(channel_id)
            if channel is None:
                raise NotFoundError(f"notification channel not found: {channel_id}")
            kind = ChannelKind(channel.kind)
            config = decrypt_config(self._cipher, channel)
            payload = sample_payload()
            row = repo.add_delivery(
                NotificationDelivery(
                    channel_id=channel.id,
                    event=payload.event.value,
                    dedupe_key=TEST_KEY,
                    title=payload.title,
                    payload=payload.to_json(),
                    status=DeliveryStatus.SENDING.value,
                    attempts=1,
                    is_test=True,
                    next_attempt_at=now,
                    claimed_at=now,
                    created_at=now,
                )
            )
            delivery_id = row.id
        error = self._send(kind, config, payload, delivery_id)
        with self._uow:
            repo = _repo(self._uow)
            stored = repo.get_delivery(delivery_id)
            channel = repo.get_channel(channel_id)
            if stored is not None:
                stored.status = DeliveryStatus.FAILED.value if error else DeliveryStatus.SENT.value
                stored.last_error = error
                stored.delivered_at = None if error else self._clock()
            if channel is not None:
                channel.last_test_at = self._clock()
                channel.last_test_ok = error is None
                self._audit(actor, "notification.channel.test", channel, {"ok": error is None})
        return error is None, error

    def _send(
        self,
        kind: ChannelKind,
        config: Mapping[str, object],
        payload: EventPayload,
        delivery_id: int,
    ) -> str | None:
        try:
            self._deliverer.deliver(kind, config, payload, delivery_id, self._clock())
        except DeliveryError as error:
            return str(error)[:300]
        except Exception as error:
            logger.warning("channel test failed", extra={"error": type(error).__name__})
            return f"unexpected error ({type(error).__name__})"
        return None

    def _seal(self, validated: ChannelConfig) -> bytes:
        return self._cipher.encrypt(json.dumps(validated.secret))

    def _audit(
        self, actor: Actor, action: str, channel: NotificationChannel, details: dict[str, object]
    ) -> None:
        details = {"name": channel.name, **details}
        target = AuditTarget("notification_channel", str(channel.id))
        add_audit_event(self._uow, AuditEntry(actor, action, target, details), self._clock)


__all__ = ["ChannelService", "decrypt_config", "sample_payload"]
