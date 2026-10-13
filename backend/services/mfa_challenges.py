"""Pending second-step logins: the proof that the password was already accepted.

After a correct password for a user with two-factor, login returns a random challenge instead
of a session. It lives in the memory of the API process (one process, like the terminal
tickets), expires after five minutes, is single use and dies after a few wrong codes. Only its
SHA-256 is kept as the key.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

from backend.domain.clock import Clock, utcnow

CHALLENGE_TTL = timedelta(minutes=5)
MAX_ATTEMPTS = 5
MAX_PENDING = 1000


@dataclass(slots=True)
class _Pending:
    user_id: int
    expires_at: datetime
    attempts: int = 0


class MfaChallengeStore:
    """Issues, inspects and consumes challenges."""

    def __init__(self, clock: Clock = utcnow) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}

    def issue(self, user_id: int) -> str:
        """Create a challenge for ``user_id``."""
        value = secrets.token_urlsafe(32)
        with self._lock:
            self._purge()
            if len(self._pending) >= MAX_PENDING:
                self._pending.pop(next(iter(self._pending)))
            self._pending[_digest(value)] = _Pending(user_id, self._clock() + CHALLENGE_TTL)
        return value

    def peek(self, value: str) -> int | None:
        """The user a live challenge belongs to, without consuming it."""
        with self._lock:
            pending = self._live(value)
            return pending.user_id if pending else None

    def fail(self, value: str) -> None:
        """Count a wrong code; the challenge is dropped after ``MAX_ATTEMPTS``."""
        with self._lock:
            pending = self._live(value)
            if pending is None:
                return
            pending.attempts += 1
            if pending.attempts >= MAX_ATTEMPTS:
                self._pending.pop(_digest(value), None)

    def consume(self, value: str) -> None:
        """Invalidate the challenge (after a successful login)."""
        with self._lock:
            self._pending.pop(_digest(value), None)

    def _live(self, value: str) -> _Pending | None:
        pending = self._pending.get(_digest(value))
        if pending is None:
            return None
        if pending.expires_at <= self._clock():
            self._pending.pop(_digest(value), None)
            return None
        return pending

    def _purge(self) -> None:
        now = self._clock()
        for key in [k for k, item in self._pending.items() if item.expires_at <= now]:
            del self._pending[key]


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()
