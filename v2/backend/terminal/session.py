"""Terminal sessions owned by the worker: lifetime, detach grace period and limits.

A session wraps one ``docker exec -it <id> /bin/sh`` on a PTY. The browser connection ("sink")
may come and go: after a disconnect the session lives on for ``idle_seconds`` so the user can
reattach with a new ticket, but never longer than ``max_seconds`` in total. Output produced while
nobody is attached is discarded; neither input nor output is ever stored or logged.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from backend.containers.base import EnvironmentTarget
from backend.domain.actor import Actor
from backend.domain.errors import ConflictError, NotFoundError, PermissionDeniedError
from backend.terminal.pty_process import PtyProcess

logger = logging.getLogger(__name__)
WATCHDOG_INTERVAL = 0.25
MAX_SESSIONS_PER_ACTOR = 3
INPUT_RATE_BYTES = 64 * 1024
INPUT_BURST_BYTES = 256 * 1024
EXIT_WAIT_SECONDS = 2.0


class OutputSink(Protocol):
    """The attached browser connection (as seen from the worker)."""

    def send_output(self, data: bytes) -> bool:
        """Deliver terminal bytes; ``False`` when the connection is gone."""

    def send_exit(self, code: int | None, reason: str) -> None:
        """Tell the client the terminal ended."""

    def close(self) -> None:
        """Close the connection."""


class TerminalListener(Protocol):
    """Hooks for auditing; they receive metadata only."""

    def opened(self, session: TerminalSession) -> None:
        """A terminal was opened."""

    def closed(
        self, session: TerminalSession, reason: str, duration: float, code: int | None
    ) -> None:
        """A terminal ended (``reason``: exited, closed, idle_timeout, absolute_timeout, ...)."""


@dataclass(frozen=True, slots=True)
class TerminalLimits:
    """Bounds of terminal sessions."""

    idle_seconds: float = 120.0
    max_seconds: float = 1800.0
    max_sessions: int = 5
    max_per_actor: int = MAX_SESSIONS_PER_ACTOR


@dataclass(frozen=True, slots=True)
class OpenSpec:
    """Everything needed to start a terminal; built after the container was authorized."""

    argv: Sequence[str]
    env: Mapping[str, str]
    rows: int
    cols: int
    actor: Actor
    target: EnvironmentTarget
    container_id: str
    container_name: str
    service: str | None
    # Runs once when the terminal ended (best effort): ``docker exec`` leaves its shell running
    # in the container when the client dies, so the opener uses this to end it.
    cleanup: Callable[[], None] | None = None


class InputLimiter:
    """A token bucket that slows a flooding client down instead of dropping input."""

    def __init__(
        self,
        rate: float = INPUT_RATE_BYTES,
        burst: float = INPUT_BURST_BYTES,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._rate = rate
        self._burst = burst
        self._tokens = burst
        self._monotonic = monotonic
        self._sleep = sleep
        self._last = monotonic()

    def wait(self, amount: int) -> None:
        """Block until ``amount`` bytes may pass."""
        while True:
            now = self._monotonic()
            self._tokens = min(self._burst, self._tokens + (now - self._last) * self._rate)
            self._last = now
            if self._tokens >= amount:
                self._tokens -= amount
                return
            self._sleep((amount - self._tokens) / self._rate)


class TerminalSession:
    """One PTY-backed terminal."""

    def __init__(
        self,
        spec: OpenSpec,
        process: PtyProcess,
        limits: TerminalLimits,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.id = secrets.token_urlsafe(12)
        self.spec = spec
        self._process = process
        self._limits = limits
        self._monotonic = monotonic
        self.opened_at = monotonic()
        self._lock = threading.Lock()
        self._sink: OutputSink | None = None
        self._detached_at: float | None = self.opened_at
        self._reason: str | None = None
        self._finished = threading.Event()
        self._on_finished: Callable[[TerminalSession, str, int | None], None] | None = None
        self._pump = threading.Thread(target=self._run, name=f"terminal-{self.id}", daemon=True)

    # --- lifetime ----------------------------------------------------------------

    def start(self, on_finished: Callable[[TerminalSession, str, int | None], None]) -> None:
        """Start pumping output; ``on_finished`` runs once when the session is over."""
        self._on_finished = on_finished
        self._pump.start()

    @property
    def finished(self) -> bool:
        """Whether the session has ended."""
        return self._finished.is_set()

    def wait_finished(self, timeout: float) -> bool:
        """Block until the session ended; ``False`` on timeout."""
        return self._finished.wait(timeout)

    def finish(self, reason: str) -> None:
        """End the session now (idempotent; the first reason wins)."""
        with self._lock:
            if self._reason is None:
                self._reason = reason
        self._process.terminate()

    def abort(self) -> None:
        """Kill a session whose pump never started and release the terminal."""
        self._process.terminate()
        self._process.close()
        self._finished.set()

    def expired_reason(self) -> str | None:
        """``absolute_timeout`` / ``idle_timeout`` when a limit was reached, else ``None``."""
        now = self._monotonic()
        with self._lock:
            if now - self.opened_at >= self._limits.max_seconds:
                return "absolute_timeout"
            detached = self._detached_at
        if detached is not None and now - detached >= self._limits.idle_seconds:
            return "idle_timeout"
        return None

    # --- connection --------------------------------------------------------------

    def attach(self, sink: OutputSink) -> None:
        """Make ``sink`` the receiver of output; a previous connection is closed."""
        with self._lock:
            previous, self._sink = self._sink, sink
            self._detached_at = None
        if previous is not None and previous is not sink:
            previous.close()

    def detach(self, sink: OutputSink) -> None:
        """The connection ended: start the grace period (only if ``sink`` is still current)."""
        with self._lock:
            if self._sink is not sink:
                return
            self._sink = None
            self._detached_at = self._monotonic()

    def write(self, data: bytes) -> None:
        """Input from the user."""
        self._process.write(data)

    def resize(self, rows: int, cols: int) -> None:
        """Window size change."""
        self._process.resize(rows, cols)

    # --- internals ---------------------------------------------------------------

    def _run(self) -> None:
        try:
            while (data := self._process.read()) is not None:
                if data:
                    self._deliver(data)
        finally:
            self._conclude()

    def _deliver(self, data: bytes) -> None:
        with self._lock:
            sink = self._sink
        if sink is not None and not sink.send_output(data):
            self.detach(sink)

    def _conclude(self) -> None:
        code = self._process.wait(EXIT_WAIT_SECONDS)
        self._process.close()
        with self._lock:
            reason = self._reason or "exited"
            self._reason = reason
            sink, self._sink = self._sink, None
        self._cleanup()
        self._finished.set()
        if sink is not None:
            sink.send_exit(code, reason)
            sink.close()
        if self._on_finished is not None:
            self._on_finished(self, reason, code)

    def _cleanup(self) -> None:
        if self.spec.cleanup is None:
            return
        try:
            self.spec.cleanup()
        except Exception:
            logger.warning("terminal cleanup failed", extra={"terminal_id": self.id})

    @property
    def age(self) -> float:
        """Seconds since the terminal was opened."""
        return self._monotonic() - self.opened_at


ProcessFactory = Callable[[OpenSpec], PtyProcess]


def default_process_factory(spec: OpenSpec) -> PtyProcess:
    """Start the PTY process of ``spec``."""
    return PtyProcess(spec.argv, spec.env, spec.rows, spec.cols)


class TerminalManager:
    """Registry of live sessions plus the watchdog that enforces the time limits."""

    def __init__(
        self,
        limits: TerminalLimits,
        listener: TerminalListener,
        process_factory: ProcessFactory = default_process_factory,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limits = limits
        self._listener = listener
        self._factory = process_factory
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._sessions: dict[str, TerminalSession] = {}
        self._stop = threading.Event()
        self._watchdog = threading.Thread(target=self._watch, name="terminal-watchdog", daemon=True)

    def start(self) -> None:
        """Start enforcing time limits."""
        self._watchdog.start()

    def create(self, spec: OpenSpec) -> TerminalSession:
        """Create a session (not yet pumping); enforces the concurrency limits."""
        with self._lock:
            live = list(self._sessions.values())
            if len(live) >= self._limits.max_sessions:
                raise ConflictError("too many open terminals")
            if sum(1 for s in live if s.spec.actor == spec.actor) >= self._limits.max_per_actor:
                raise ConflictError("too many open terminals for this user")
            session = TerminalSession(spec, self._factory(spec), self._limits, self._monotonic)
            self._sessions[session.id] = session
        return session

    def begin(self, session: TerminalSession, sink: OutputSink) -> None:
        """Audit the open, attach the first connection and start the session.

        When the audit fails the terminal is not opened at all.
        """
        try:
            self._listener.opened(session)
        except BaseException:
            self.discard(session)
            raise
        session.attach(sink)
        session.start(self._finished)

    def discard(self, session: TerminalSession) -> None:
        """Throw away a session that never started pumping (its process is killed)."""
        with self._lock:
            self._sessions.pop(session.id, None)
        session.abort()

    def reattach(self, terminal_id: str, actor: Actor, sink: OutputSink) -> TerminalSession:
        """Attach a new connection to a live session of the same actor."""
        with self._lock:
            session = self._sessions.get(terminal_id)
        if session is None or session.finished:
            raise NotFoundError("terminal not found or already closed")
        if session.spec.actor != actor:
            raise PermissionDeniedError("this terminal belongs to someone else")
        session.attach(sink)
        return session

    def lookup(self, terminal_id: str, actor: Actor) -> TerminalSession:
        """The live session, for the actor that owns it."""
        with self._lock:
            session = self._sessions.get(terminal_id)
        if session is None or session.finished:
            raise NotFoundError("terminal not found or already closed")
        if session.spec.actor != actor:
            raise PermissionDeniedError("this terminal belongs to someone else")
        return session

    def count(self) -> int:
        """Number of live sessions."""
        with self._lock:
            return len(self._sessions)

    def shutdown(self) -> None:
        """End every session and stop the watchdog."""
        self._stop.set()
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            session.finish("shutdown")
        for session in sessions:
            session.wait_finished(EXIT_WAIT_SECONDS * 3)
        if self._watchdog.is_alive():
            self._watchdog.join(5)

    def _finished(self, session: TerminalSession, reason: str, code: int | None) -> None:
        with self._lock:
            self._sessions.pop(session.id, None)
        try:
            self._listener.closed(session, reason, session.age, code)
        except Exception:
            logger.exception("terminal close audit failed", extra={"terminal_id": session.id})

    def _watch(self) -> None:
        while not self._stop.wait(WATCHDOG_INTERVAL):
            with self._lock:
                sessions = list(self._sessions.values())
            for session in sessions:
                reason = session.expired_reason()
                if reason is not None and not session.finished:
                    session.finish(reason)
