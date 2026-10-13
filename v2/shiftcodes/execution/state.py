"""Outcome bookkeeping shared by the threads of one run."""

from __future__ import annotations

import threading
from typing import Protocol

from shiftcodes.domain.enums import RunStatus


class RunState:
    """Whether anything failed so far, and the first failure message."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._failed = False
        self._message: str | None = None

    def fail(self, message: str) -> None:
        """Record a failure; the first message is kept."""
        with self._lock:
            self._failed = True
            if self._message is None:
                self._message = message

    def snapshot(self) -> tuple[bool, str | None]:
        """The current state, to ``restore`` when an attempt is retried."""
        with self._lock:
            return self._failed, self._message

    def restore(self, snapshot: tuple[bool, str | None]) -> None:
        """Return to an earlier ``snapshot``."""
        with self._lock:
            self._failed, self._message = snapshot

    def reset(self) -> None:
        """Forget earlier failures (a retried pipeline starts over)."""
        with self._lock:
            self._failed = False
            self._message = None

    @property
    def failed(self) -> bool:
        """Whether a failure was recorded."""
        return self._failed

    @property
    def message(self) -> str | None:
        """The first failure message."""
        return self._message


class RunHistory(Protocol):
    """Earlier runs of the same environment, for the ``changed`` post condition."""

    def previous_run_status(self) -> RunStatus | None:
        """Status of the latest finished run before this one."""

    def previous_stage_status(self, stage_name: str) -> RunStatus | None:
        """Status of the same stage in that run."""
