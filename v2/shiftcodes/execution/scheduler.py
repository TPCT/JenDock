"""The run queue: claims queued runs oldest first and executes them on threads."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Protocol

from shiftcodes.execution.cancel import CancelReason, CancelToken
from shiftcodes.execution.environment import ExecutionEnv
from shiftcodes.execution.run_executor import RunExecutor

logger = logging.getLogger(__name__)
SHUTDOWN_GRACE = 30.0


class StopSignal(Protocol):
    """``ShutdownHandler`` as the loop sees it."""

    def wait(self, timeout: float) -> bool:
        """Sleep up to ``timeout``; return True when shutdown was requested."""


@dataclass(slots=True)
class _Active:
    thread: threading.Thread
    token: CancelToken


class RunScheduler:
    """Starts at most ``max_concurrent_runs`` runs at once (a global limit of this worker)."""

    def __init__(self, env: ExecutionEnv) -> None:
        self._env = env
        self._executor = RunExecutor(env)
        self._active: dict[int, _Active] = {}
        self._lock = threading.Lock()

    @property
    def env(self) -> ExecutionEnv:
        """The execution environment (used for recovery at start)."""
        return self._env

    @property
    def active_run_ids(self) -> list[int]:
        """Runs currently executing."""
        with self._lock:
            self._reap()
            return sorted(self._active)

    def tick(self) -> list[int]:
        """Claim as many queued runs as there are free slots; return the claimed ids."""
        claimed: list[int] = []
        with self._lock:
            self._reap()
            free = self._env.config.max_concurrent_runs - len(self._active)
            lifecycle = self._env.lifecycle()
            for _ in range(max(0, free)):
                run = lifecycle.claim_next()
                if run is None:
                    break
                self._start(run.id)
                claimed.append(run.id)
        return claimed

    def _start(self, run_id: int) -> None:
        token = CancelToken()
        thread = threading.Thread(
            target=self._execute, args=(run_id, token), name=f"run-{run_id}", daemon=True
        )
        self._active[run_id] = _Active(thread, token)
        thread.start()

    def _execute(self, run_id: int, token: CancelToken) -> None:
        try:
            outcome = self._executor.execute(run_id, token)
            logger.info("run finished", extra={"run_id": run_id, "status": outcome.status.value})
        except Exception:
            logger.exception("run could not be finished", extra={"run_id": run_id})

    def _reap(self) -> None:
        for run_id in [rid for rid, item in self._active.items() if not item.thread.is_alive()]:
            del self._active[run_id]

    def run_forever(self, stop: StopSignal) -> None:
        """Poll the queue until ``stop`` fires, then interrupt running runs and wait for them."""
        interval = self._env.config.queue_poll_interval
        while True:
            try:
                self.tick()
            except Exception:
                logger.exception("queue poll failed")
            if stop.wait(interval):
                break
        self.shutdown()

    def shutdown(self, grace: float = SHUTDOWN_GRACE) -> None:
        """Ask every running run to stop (status ``interrupted``) and wait up to ``grace``."""
        with self._lock:
            items = list(self._active.values())
        for item in items:
            item.token.cancel(CancelReason.SHUTDOWN)
        for item in items:
            item.thread.join(grace)

    def wait_idle(self, timeout: float) -> bool:
        """Wait until no run is executing; return False on timeout."""
        with self._lock:
            threads = [item.thread for item in self._active.values()]
        for thread in threads:
            thread.join(timeout)
        return not self.active_run_ids
