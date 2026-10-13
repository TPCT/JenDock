"""Cancellation of one run."""

from __future__ import annotations

import threading
from enum import StrEnum


class CancelReason(StrEnum):
    """Why a run is being stopped."""

    CANCELLED = "cancelled"  # a user asked for it
    SHUTDOWN = "shutdown"  # the worker is stopping


class CancelToken:
    """Shared by everything that executes one run; implements ``CancelSignal``.

    Once the post phase starts, cancellation no longer interrupts anything: post steps are
    the cleanup that a cancelled run still owes (``aborted`` and ``cleanup``).
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason: CancelReason | None = None
        self._post_phase = False
        self._lock = threading.Lock()

    def cancel(self, reason: CancelReason) -> None:
        """Request cancellation; the first reason wins."""
        with self._lock:
            if self._reason is None:
                self._reason = reason
        self._event.set()

    def is_set(self) -> bool:
        """Whether cancellation was requested."""
        return self._event.is_set()

    @property
    def reason(self) -> CancelReason | None:
        """Why the run is cancelled, if it is."""
        return self._reason

    def wait(self, timeout: float) -> bool:
        """Sleep up to ``timeout`` seconds; return True when cancellation arrived."""
        return self._event.wait(timeout)

    def enter_post_phase(self) -> None:
        """From now on cancellation does not interrupt steps."""
        self._post_phase = True

    @property
    def interrupting(self) -> bool:
        """Whether a cancel should currently stop steps and kill containers."""
        return self._event.is_set() and not self._post_phase


class NeverCancelled:
    """A ``CancelSignal`` that never fires (post steps)."""

    def is_set(self) -> bool:
        """Always False."""
        return False
