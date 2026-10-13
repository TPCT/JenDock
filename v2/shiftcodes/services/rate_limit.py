"""In-memory sliding-window limiter for failed logins.

State lives in process memory: correct for the single API process this platform runs,
reset by a restart, and not shared between processes. Move it to the database or a
shared store before running more than one API replica.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import timedelta
from math import ceil

from shiftcodes.domain.clock import Clock, utcnow
from shiftcodes.domain.errors import RateLimitedError

LOGIN_WINDOW = timedelta(minutes=15)
MAX_FAILURES_PER_USERNAME = 5
MAX_FAILURES_PER_IP = 20
MAX_TRACKED_KEYS = 10_000


class LoginRateLimiter:
    """Counts failed attempts per username and per client IP."""

    def __init__(
        self,
        clock: Clock = utcnow,
        window: timedelta = LOGIN_WINDOW,
        max_per_username: int = MAX_FAILURES_PER_USERNAME,
        max_per_ip: int = MAX_FAILURES_PER_IP,
    ) -> None:
        self._clock = clock
        self._window = window
        self._limits = {"user": max_per_username, "ip": max_per_ip}
        self._failures: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, username: str, client_ip: str) -> None:
        """Raise ``RateLimitedError`` if either key is over its limit."""
        now = self._now()
        with self._lock:
            waits = [
                self._retry_after(key, now)
                for key in (("user", username.lower()), ("ip", client_ip))
            ]
        blocked = [wait for wait in waits if wait > 0]
        if blocked:
            raise RateLimitedError("too many failed login attempts", max(blocked))

    def record_failure(self, username: str, client_ip: str) -> None:
        """Count one failed attempt against the username and the IP."""
        now = self._now()
        with self._lock:
            if len(self._failures) >= MAX_TRACKED_KEYS:
                self._sweep(now)
            for key in (("user", username.lower()), ("ip", client_ip)):
                self._failures.setdefault(key, deque()).append(now)

    def reset_username(self, username: str) -> None:
        """Forget the failures of a username after a successful login."""
        with self._lock:
            self._failures.pop(("user", username.lower()), None)

    def _now(self) -> float:
        return self._clock().timestamp()

    def _retry_after(self, key: tuple[str, str], now: float) -> int:
        attempts = self._failures.get(key)
        if attempts is None:
            return 0
        self._expire(attempts, now)
        if len(attempts) < self._limits[key[0]]:
            return 0
        return max(1, ceil(attempts[0] + self._window.total_seconds() - now))

    def _expire(self, attempts: deque[float], now: float) -> None:
        horizon = now - self._window.total_seconds()
        while attempts and attempts[0] <= horizon:
            attempts.popleft()

    def _sweep(self, now: float) -> None:
        for key in list(self._failures):
            self._expire(self._failures[key], now)
            if not self._failures[key]:
                del self._failures[key]
        while len(self._failures) >= MAX_TRACKED_KEYS:
            del self._failures[next(iter(self._failures))]
