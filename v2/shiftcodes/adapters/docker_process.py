"""Running a docker CLI process: line streaming, timeout and cancellation."""

from __future__ import annotations

import contextlib
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from typing import IO

from shiftcodes.adapters.container_runtime import CancelSignal, ExecResult, LineSink

MAX_LINE_BYTES = 64 * 1024
WATCH_INTERVAL = 0.1
TERMINATE_GRACE = 5.0
OnFirstLine = Callable[[], None]


class _Watchdog(threading.Thread):
    """Ends a process when its deadline passes or cancellation is requested."""

    def __init__(
        self,
        process: subprocess.Popen[bytes],
        timeout: float | None,
        cancel: CancelSignal | None,
        on_abort: Callable[[], None] | None,
    ) -> None:
        super().__init__(name="docker-watchdog", daemon=True)
        self._process = process
        self._deadline = None if timeout is None else time.monotonic() + timeout
        self._cancel = cancel
        self._on_abort = on_abort
        self._stopped = threading.Event()
        self.timed_out = False
        self.cancelled = False

    def run(self) -> None:
        while not self._stopped.wait(WATCH_INTERVAL):
            if self._cancel is not None and self._cancel.is_set():
                self.cancelled = True
            elif self._deadline is not None and time.monotonic() >= self._deadline:
                self.timed_out = True
            else:
                continue
            self._abort()
            return

    def _abort(self) -> None:
        if self._on_abort is not None:
            with contextlib.suppress(Exception):
                self._on_abort()
        self._process.terminate()
        try:
            self._process.wait(TERMINATE_GRACE)
        except subprocess.TimeoutExpired:
            self._process.kill()

    def stop(self) -> None:
        """Stop watching (the process ended by itself)."""
        self._stopped.set()


def _read_line(stream: IO[bytes]) -> str | None:
    """One line (at most ``MAX_LINE_BYTES``; longer lines arrive in pieces), or ``None`` at EOF."""
    raw = stream.readline(MAX_LINE_BYTES)
    if not raw:
        return None
    return raw.decode("utf-8", errors="replace").rstrip("\r\n")


def stream_process(  # noqa: PLR0913 - a streamed process genuinely has these knobs
    argv: list[str],
    env: Mapping[str, str],
    sink: LineSink,
    *,
    timeout: float | None,
    cancel: CancelSignal | None,
    on_abort: Callable[[], None] | None = None,
    on_started: Callable[[subprocess.Popen[bytes]], None] | None = None,
    on_first_line: OnFirstLine | None = None,
) -> ExecResult:
    """Run ``argv`` and feed every output line (stdout and stderr merged) to ``sink``."""
    process = subprocess.Popen(  # noqa: S603 - explicit argv list, never a shell
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=dict(env),
        start_new_session=True,
    )
    watchdog = _Watchdog(process, timeout, cancel, on_abort)
    watchdog.start()
    try:
        if on_started is not None:
            on_started(process)
        _pump(process, sink, on_first_line)
    except BaseException:
        process.kill()
        raise
    finally:
        watchdog.stop()
        watchdog.join(TERMINATE_GRACE + 1)
        if process.stdout is not None:
            process.stdout.close()
        process.wait()
    return ExecResult(process.returncode, watchdog.timed_out, watchdog.cancelled)


def _pump(process: subprocess.Popen[bytes], sink: LineSink, first: OnFirstLine | None) -> None:
    stream = process.stdout
    if stream is None:
        raise RuntimeError("process has no stdout pipe")
    seen = False
    while (line := _read_line(stream)) is not None:
        if not seen and first is not None:
            first()
        seen = True
        sink(line)
