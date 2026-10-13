"""One lock per environment, shared by deployments and container operations.

The worker process runs both the run executor and the RPC server, so a plain in-process lock is
enough. Container operations wait a bounded time and then report a conflict; a deployment waits
until the operation ends (or the run is cancelled).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

from shiftcodes.adapters.container_runtime import CancelSignal, LineSink
from shiftcodes.adapters.deployer import Deployer, DeployRequest
from shiftcodes.domain.errors import ConflictError

POLL_SECONDS = 0.2


class EnvironmentBusyError(ConflictError):
    """Another operation (a deployment or a container change) holds the environment."""


class EnvironmentLocks:
    """Named locks created on first use."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}

    def _lock(self, key: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    @contextmanager
    def hold(
        self, key: str, *, timeout: float | None, cancel: CancelSignal | None = None
    ) -> Iterator[None]:
        """Hold the lock of ``key``.

        ``timeout`` is how long to wait (``None``: until acquired). When ``cancel`` becomes set
        or the time is up, ``EnvironmentBusyError`` is raised.
        """
        lock = self._lock(key)
        waited = 0.0
        while not lock.acquire(timeout=POLL_SECONDS):
            waited += POLL_SECONDS
            cancelled = cancel is not None and cancel.is_set()
            if cancelled or (timeout is not None and waited >= timeout):
                raise EnvironmentBusyError(
                    "another operation is changing this environment, try again shortly"
                )
        try:
            yield
        finally:
            lock.release()

    def is_held(self, key: str) -> bool:
        """Whether someone currently holds ``key`` (for tests and diagnostics)."""
        lock = self._lock(key)
        if lock.acquire(blocking=False):
            lock.release()
            return False
        return True


def lock_key(project: str, environment: str) -> str:
    """The lock name of an environment: ``<project slug>/<environment>``."""
    return f"{project}/{environment}"


class LockedDeployer:
    """Wraps a ``Deployer`` so a deployment excludes container operations on its environment."""

    def __init__(self, inner: Deployer, locks: EnvironmentLocks) -> None:
        self._inner = inner
        self._locks = locks

    def deploy(self, request: DeployRequest, sink: LineSink, cancel: CancelSignal) -> None:
        """Wait for the environment lock (observing cancellation), then deploy."""
        key = lock_key(request.project, request.environment)
        with self._locks.hold(key, timeout=None, cancel=cancel):
            self._inner.deploy(request, sink, cancel)
