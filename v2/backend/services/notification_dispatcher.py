"""The outbox dispatcher: sends queued notifications from the worker, with retries.

Delivery is asynchronous and durable. A row is claimed (``sending``, one more attempt counted)
before the network is touched, so a worker that dies mid-send leaves a row that is picked up
again after ``stale_seconds``. A failed attempt schedules the next one with exponential
backoff (30 s, 1 min, 2 min, ... capped at one hour) until ``max_attempts`` is reached; then the
row stays as ``failed`` with the last error for the delivery history.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from backend.adapters.notify_senders import Deliverer, DeliveryError
from backend.domain.clock import Clock, utcnow
from backend.domain.errors import ConfigurationError
from backend.domain.notifications import ChannelKind, DeliveryStatus, EventPayload
from backend.repositories.database import UnitOfWork
from backend.repositories.notifications import NotificationRepository
from backend.services.notification_channels import decrypt_config
from backend.services.secret_cipher import SecretCipher

logger = logging.getLogger(__name__)
MAX_BACKOFF = timedelta(hours=1)
PRUNE_EVERY = timedelta(hours=1)
MAX_ERROR = 300


class StopSignal(Protocol):
    """``ShutdownHandler`` as the loop sees it."""

    def wait(self, timeout: float) -> bool:
        """Sleep up to ``timeout``; return True when shutdown was requested."""


@dataclass(frozen=True, slots=True)
class DispatchPolicy:
    """Retry and housekeeping tunables."""

    max_attempts: int = 7
    retry_base_seconds: float = 30.0
    batch: int = 20
    stale_seconds: float = 300.0
    history_days: int = 30
    poll_seconds: float = 2.0


def backoff(attempt: int, base_seconds: float) -> timedelta:
    """The wait after failed attempt number ``attempt`` (1-based)."""
    return min(timedelta(seconds=base_seconds * 2 ** max(attempt - 1, 0)), MAX_BACKOFF)


@dataclass(frozen=True, slots=True)
class _Claimed:
    delivery_id: int
    channel_id: int
    payload: EventPayload
    attempts: int


class NotificationDispatcher:
    """Claims due rows and delivers them one at a time."""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        cipher: SecretCipher,
        deliverer: Deliverer,
        policy: DispatchPolicy = DispatchPolicy(),  # noqa: B008 - frozen value object
        clock: Clock = utcnow,
    ) -> None:
        self._uow_factory = uow_factory
        self._cipher = cipher
        self._deliverer = deliverer
        self._policy = policy
        self._clock = clock
        self._last_prune = clock() - PRUNE_EVERY

    def run_once(self) -> int:
        """Deliver everything that is due now; returns how many rows were attempted."""
        claimed = self._claim()
        for item in claimed:
            self._attempt(item)
        return len(claimed)

    def run_forever(self, stop: StopSignal) -> None:
        """Dispatch until ``stop`` fires; one failing pass never ends the loop."""
        while True:
            attempted = 0
            try:
                attempted = self.run_once()
                self.prune()
            except Exception:
                logger.exception("notification dispatch pass failed")
            if stop.wait(0 if attempted else self._policy.poll_seconds):
                return

    # --- internals --------------------------------------------------------------------------

    def _claim(self) -> list[_Claimed]:
        now = self._clock()
        stale = now - timedelta(seconds=self._policy.stale_seconds)
        claimed: list[_Claimed] = []
        with self._uow_factory() as uow:
            repo = NotificationRepository(uow.session)
            for row in repo.due(now, stale, self._policy.batch):
                row.status = DeliveryStatus.SENDING.value
                row.attempts += 1
                row.claimed_at = now
                claimed.append(
                    _Claimed(
                        row.id, row.channel_id, EventPayload.from_json(row.payload), row.attempts
                    )
                )
        return claimed

    def _attempt(self, item: _Claimed) -> None:
        error: str | None = None
        permanent = False
        try:
            kind, config = self._load(item.channel_id)
            self._deliverer.deliver(kind, config, item.payload, item.delivery_id, self._clock())
        except DeliveryError as failure:
            error = str(failure)
        except _UnusableError as failure:
            error, permanent = str(failure), True
        except Exception as failure:
            logger.warning("notification delivery crashed", extra={"error": type(failure).__name__})
            error = f"unexpected error ({type(failure).__name__})"
        self._record(item, error, permanent=permanent)

    def _load(self, channel_id: int) -> tuple[ChannelKind, dict[str, object]]:
        with self._uow_factory() as uow:
            channel = NotificationRepository(uow.session).get_channel_with_config(channel_id)
            if channel is None:
                raise _UnusableError("the channel no longer exists")
            if not channel.enabled:
                raise _UnusableError("the channel is disabled")
            try:
                return ChannelKind(channel.kind), decrypt_config(self._cipher, channel)
            except ConfigurationError as failure:
                raise _UnusableError("the channel configuration cannot be read") from failure

    def _record(self, item: _Claimed, error: str | None, *, permanent: bool) -> None:
        now = self._clock()
        with self._uow_factory() as uow:
            row = NotificationRepository(uow.session).get_delivery(item.delivery_id)
            if row is None:
                return
            if error is None:
                row.status = DeliveryStatus.SENT.value
                row.last_error = None
                row.delivered_at = now
            elif permanent or item.attempts >= self._policy.max_attempts:
                row.status = DeliveryStatus.FAILED.value
                row.last_error = error[:MAX_ERROR]
            else:
                row.status = DeliveryStatus.PENDING.value
                row.last_error = error[:MAX_ERROR]
                row.next_attempt_at = now + backoff(item.attempts, self._policy.retry_base_seconds)

    def prune(self) -> None:
        """Delete finished deliveries older than the history window (at most hourly)."""
        now = self._clock()
        if now - self._last_prune < PRUNE_EVERY:
            return
        self._last_prune = now
        cutoff = now - timedelta(days=self._policy.history_days)
        with self._uow_factory() as uow:
            NotificationRepository(uow.session).prune_before(cutoff)


class _UnusableError(Exception):
    """The delivery cannot succeed however often it is retried."""
