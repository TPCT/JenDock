"""Single-use confirmation tickets for the two-step lifecycle actions."""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

from shiftcodes.domain.actor import Actor
from shiftcodes.domain.clock import Clock, utcnow
from shiftcodes.domain.errors import ValidationError

TTL = timedelta(seconds=60)
MAX_PENDING = 500


@dataclass(frozen=True, slots=True)
class _Pending:
    actor: Actor
    scope: tuple[str, ...]
    expires_at: datetime


class ConfirmationStore:
    """In-memory tickets bound to actor and scope (environment, container id, action).

    They live in the worker, which executes the action, so a restart simply invalidates them.
    A ticket works once, for the actor that requested it, within 60 seconds.
    """

    def __init__(self, clock: Clock = utcnow) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}

    def issue(self, actor: Actor, scope: tuple[str, ...]) -> tuple[str, datetime]:
        """Create a ticket; the oldest ones are dropped beyond ``MAX_PENDING``."""
        value = secrets.token_urlsafe(24)
        expires_at = self._clock() + TTL
        with self._lock:
            self._purge()
            while len(self._pending) >= MAX_PENDING:
                self._pending.pop(next(iter(self._pending)))
            self._pending[value] = _Pending(actor, scope, expires_at)
        return value, expires_at

    def consume(self, value: str, actor: Actor, prefix: tuple[str, ...]) -> tuple[str, ...]:
        """Use a ticket and return the scope it was issued for.

        The scope must start with ``prefix``; any mismatch, expiry or reuse raises
        ``ValidationError``.
        """
        with self._lock:
            pending = self._pending.pop(value, None)
        valid = (
            pending is not None
            and pending.actor == actor
            and pending.scope[: len(prefix)] == prefix
            and pending.expires_at > self._clock()
        )
        if pending is None or not valid:
            raise ValidationError("confirmation is invalid, expired or was already used")
        return pending.scope

    def _purge(self) -> None:
        now = self._clock()
        for value in [v for v, item in self._pending.items() if item.expires_at <= now]:
            del self._pending[value]
