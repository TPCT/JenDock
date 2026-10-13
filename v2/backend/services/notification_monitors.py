"""Monitors behind the two global events: disk usage and webhook signature failures."""

from __future__ import annotations

import logging
import shutil
import threading
from collections import deque
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple, Protocol

from backend.domain.clock import Clock, utcnow
from backend.domain.notifications import EventPayload, NotificationEvent
from backend.services.notification_events import safely
from backend.services.notification_outbox import EventSink

logger = logging.getLogger(__name__)
GIB = 1024**3


class DiskUsage(NamedTuple):
    """``shutil.disk_usage`` as a value."""

    total: int
    used: int
    free: int


class StopSignal(Protocol):
    """``ShutdownHandler`` as the loop sees it."""

    def wait(self, timeout: float) -> bool:
        """Sleep up to ``timeout``; return True when shutdown was requested."""


class DiskMonitor:
    """Announces when the data volume is fuller than the threshold.

    ``tick`` does one check (tests call it); ``run_forever`` repeats it. The quiet period of the
    event keeps a full disk from producing a message every check.
    """

    def __init__(
        self,
        path: Path,
        threshold_percent: int,
        sink: EventSink,
        interval: float,
        usage: Callable[[Path], DiskUsage] | None = None,
    ) -> None:
        self._path = path
        self._threshold = threshold_percent
        self._sink = sink
        self._interval = interval
        self._usage = usage or _real_usage

    def tick(self) -> bool:
        """Check once; return whether usage is above the threshold."""
        usage = self._usage(self._path)
        percent = round(usage.used * 100 / usage.total) if usage.total else 0
        if percent < self._threshold:
            return False
        details = {
            "Used": f"{percent}%",
            "Free": f"{usage.free / GIB:.1f} GiB",
            "Threshold": f"{self._threshold}%",
        }
        self._sink.emit(
            EventPayload(
                event=NotificationEvent.DISK_USAGE_HIGH,
                dedupe_key="disk.usage_high:data",
                title=f"Disk usage is {percent}% on the Jendock data volume",
                message="Old images, workspaces or logs may need cleaning up.",
                link_path="/settings/notifications",
                details=details,
            )
        )
        return True

    def run_forever(self, stop: StopSignal) -> None:
        """Check periodically until shutdown; a failing check never ends the loop."""
        while True:
            safely(self.tick, "disk")
            if stop.wait(self._interval):
                return


def _real_usage(path: Path) -> DiskUsage:
    usage = shutil.disk_usage(path)
    return DiskUsage(usage.total, usage.used, usage.free)


class WebhookFailureTracker:
    """Counts invalid webhook signatures and announces a spike.

    Thread-safe (RPC calls run on several threads). A spike is more than ``threshold`` failures
    within ``window``; the event's quiet period collapses repeats while it lasts.
    """

    def __init__(
        self,
        sink: EventSink,
        threshold: int,
        window: timedelta,
        clock: Clock = utcnow,
    ) -> None:
        self._sink = sink
        self._threshold = threshold
        self._window = window
        self._clock = clock
        self._failures: deque[datetime] = deque()
        self._lock = threading.Lock()

    def record_failure(self) -> None:
        """Count one invalid signature; never raises."""
        now = self._clock()
        with self._lock:
            self._failures.append(now)
            while self._failures and now - self._failures[0] > self._window:
                self._failures.popleft()
            count = len(self._failures)
        if count > self._threshold:
            safely(lambda: self._announce(count), "webhook")

    def _announce(self, count: int) -> None:
        minutes = int(self._window.total_seconds() // 60)
        self._sink.emit(
            EventPayload(
                event=NotificationEvent.WEBHOOK_SIGNATURE_SPIKE,
                dedupe_key="webhook.signature_spike",
                title=f"{count} invalid GitHub webhook signatures in {minutes} minutes",
                message=(
                    "Someone may be probing the webhook endpoint, or the secret differs "
                    "from the one configured on GitHub."
                ),
                link_path="/settings/notifications",
                details={"Failures": str(count), "Window": f"{minutes} minutes"},
            )
        )
