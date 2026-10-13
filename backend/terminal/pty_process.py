"""A child process on a pseudo terminal (stdlib ``pty``/``os``; never a shell string)."""

from __future__ import annotations

import contextlib
import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import termios
import threading
import time
from collections.abc import Mapping, Sequence

READ_CHUNK = 4096
POLL_SECONDS = 0.2
KILL_GRACE = 2.0
WRITE_DEADLINE = 5.0
MIN_SIZE = 1
MAX_SIZE = 500


def clamp_size(rows: int, cols: int) -> tuple[int, int]:
    """Keep terminal dimensions in a sane range."""
    return max(MIN_SIZE, min(rows, MAX_SIZE)), max(MIN_SIZE, min(cols, MAX_SIZE))


def _set_window_size(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


class PtyProcess:
    """Runs ``argv`` with the slave side of a new PTY as its stdin, stdout and stderr."""

    def __init__(self, argv: Sequence[str], env: Mapping[str, str], rows: int, cols: int) -> None:
        rows, cols = clamp_size(rows, cols)
        self._master, slave = pty.openpty()
        os.set_blocking(self._master, False)
        self._lock = threading.Lock()
        try:
            _set_window_size(slave, rows, cols)
            self._process = subprocess.Popen(  # noqa: S603 - explicit argv, never a shell
                list(argv),
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=dict(env),
                close_fds=True,
                start_new_session=True,
            )
        except BaseException:
            os.close(self._master)
            raise
        finally:
            os.close(slave)
        self._closed = False

    def read(self) -> bytes | None:
        """Up to ``READ_CHUNK`` bytes; ``b""`` when nothing arrived in time, ``None`` at EOF.

        Only the one pump thread reads and, when done, closes the terminal.
        """
        try:
            ready, _w, _x = select.select([self._master], [], [], POLL_SECONDS)
            if not ready:
                return b""
            data = os.read(self._master, READ_CHUNK)
        except BlockingIOError:
            return b""
        except (OSError, ValueError):
            return None
        return data or None

    def write(self, data: bytes) -> None:
        """Send input to the child; a closed terminal (or one that stays full) drops it."""
        view = memoryview(data)
        deadline = time.monotonic() + WRITE_DEADLINE
        while view and time.monotonic() < deadline:
            with self._lock:
                if self._closed:
                    return
                try:
                    written = os.write(self._master, view)
                except BlockingIOError:
                    written = 0
                except OSError:
                    return
            view = view[written:]
            if written == 0:
                select.select([], [self._master], [], 0.2)

    def resize(self, rows: int, cols: int) -> None:
        """Change the window size and tell the client process (``docker exec`` follows it)."""
        rows, cols = clamp_size(rows, cols)
        with self._lock, contextlib.suppress(OSError):
            if not self._closed:
                _set_window_size(self._master, rows, cols)
        with contextlib.suppress(ProcessLookupError):
            self._process.send_signal(signal.SIGWINCH)

    def exit_code(self) -> int | None:
        """The exit code once the process ended, otherwise ``None``."""
        return self._process.poll()

    def wait(self, timeout: float) -> int | None:
        """Wait for the exit code up to ``timeout`` seconds."""
        try:
            return self._process.wait(timeout)
        except subprocess.TimeoutExpired:
            return None

    def terminate(self) -> None:
        """Stop the process (SIGHUP, then SIGKILL); the pump thread then sees EOF and closes."""
        if self._process.poll() is None:
            self._signal(signal.SIGHUP)
            if self.wait(KILL_GRACE) is None:
                self._signal(signal.SIGKILL)
                self.wait(KILL_GRACE)

    def close(self) -> None:
        """Close the master side (idempotent; called by the pump thread when it is done)."""
        with self._lock:
            if not self._closed:
                self._closed = True
                with contextlib.suppress(OSError):
                    os.close(self._master)

    def _signal(self, number: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self._process.pid, number)
