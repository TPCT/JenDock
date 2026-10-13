"""Step logs: bounded files with secrets masked before anything is written."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from shiftcodes.logging import SecretRedactor

LOG_FILE_MODE = 0o640
LOG_DIR_MODE = 0o750
CHUNK_BYTES = 64 * 1024
TRUNCATION_MARKER = "[output truncated: the log limit of this step was reached]"
MIN_LINE_SECRET = 6


class RunRedactor:
    """Thread-safe secret masking for one run (parallel branches resolve secrets concurrently).

    A multi-line secret (for example a private key) is masked line by line as well, because
    logs are masked one line at a time.
    """

    def __init__(self) -> None:
        self._inner = SecretRedactor()
        self._lock = threading.Lock()

    def register(self, secret: str) -> None:
        """Mask ``secret`` (and each substantial line of it) from now on."""
        with self._lock:
            self._inner.register(secret)
            lines = [line.strip() for line in secret.splitlines()]
            for line in lines:
                if len(line) >= MIN_LINE_SECRET:
                    self._inner.register(line)

    def redact(self, text: str) -> str:
        """Return ``text`` with registered secrets replaced."""
        with self._lock:
            return self._inner.redact_text(text)


class StepLogWriter:
    """Appends masked lines to one step's log file, bounded to ``max_bytes``."""

    def __init__(self, path: Path, redactor: RunRedactor, max_bytes: int) -> None:
        self._redactor = redactor
        self._max_bytes = max_bytes
        self._written = 0
        self._truncated = False
        self._lock = threading.Lock()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, LOG_FILE_MODE)
        os.fchmod(fd, LOG_FILE_MODE)
        self._file: IO[bytes] = os.fdopen(fd, "ab", buffering=0)

    def write_line(self, text: str) -> None:
        """Mask and append one line; after the bound only a single truncation marker is kept."""
        data = (self._redactor.redact(text) + "\n").encode("utf-8", errors="replace")
        with self._lock:
            if self._truncated or self._file.closed:
                return
            if self._written + len(data) > self._max_bytes:
                self._truncated = True
                data = (TRUNCATION_MARKER + "\n").encode()
            self._file.write(data)
            self._written += len(data)

    def close(self) -> None:
        """Close the file."""
        with self._lock:
            self._file.close()


@dataclass(frozen=True, slots=True)
class LogChunk:
    """A slice of a log: ``text`` starts at byte ``offset`` and the next read starts at
    ``next_offset``; ``size`` is the current file size."""

    offset: int
    next_offset: int
    size: int
    text: str

    @property
    def at_end(self) -> bool:
        """Whether the chunk reaches the current end of the file."""
        return self.next_offset >= self.size


class LogStore:
    """Layout and access of ``<root>/<run_id>/<step_id>.log``."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def path(self, run_id: int, step_id: int) -> Path:
        """The log file of a step."""
        return self._root / str(run_id) / f"{step_id}.log"

    def open_writer(
        self, run_id: int, step_id: int, redactor: RunRedactor, max_bytes: int
    ) -> StepLogWriter:
        """Create the step's log file (0640, in a 0750 directory) and return its writer."""
        directory = self._root / str(run_id)
        directory.mkdir(parents=True, exist_ok=True, mode=LOG_DIR_MODE)
        directory.chmod(LOG_DIR_MODE)
        return StepLogWriter(self.path(run_id, step_id), redactor, max_bytes)

    def read_chunk(
        self, run_id: int, step_id: int, offset: int = 0, limit: int = CHUNK_BYTES
    ) -> LogChunk:
        """Read up to ``limit`` bytes from ``offset``, ending on a line (or character) boundary."""
        path = self.path(run_id, step_id)
        try:
            size = path.stat().st_size
            with path.open("rb") as handle:
                handle.seek(min(max(offset, 0), size))
                start = handle.tell()
                data = handle.read(limit)
        except FileNotFoundError:
            return LogChunk(0, 0, 0, "")
        if start + len(data) < size:
            data = _whole_lines(data)
        return LogChunk(start, start + len(data), size, data.decode("utf-8", errors="replace"))


def _whole_lines(data: bytes) -> bytes:
    """Cut ``data`` after its last newline, or at a character boundary when it has none."""
    cut = data.rfind(b"\n")
    if cut >= 0:
        return data[: cut + 1]
    for trim in range(min(3, len(data))):
        end = len(data) - trim
        try:
            data[:end].decode("utf-8")
        except UnicodeDecodeError:
            continue
        return data[:end]
    return data
