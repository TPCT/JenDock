"""Runs database dump clients in a short-lived environment-network container."""

from __future__ import annotations

import contextlib
import gzip
import queue
import secrets
import threading
from dataclasses import dataclass
from typing import BinaryIO, Protocol

from backend.adapters.container_runtime import BinarySink, ExecResult, RunSpec
from backend.domain.database import DatabaseEngine
from backend.domain.database_backups import BackupError

_END = object()


class DumpRuntime(Protocol):
    def image_exists(self, image: str) -> bool: ...

    def run_container_binary(
        self, spec: RunSpec, sink: BinarySink, *, timeout: float | None = None
    ) -> ExecResult: ...

    def kill_by_label(self, label: str) -> int: ...


class UploadStore(Protocol):
    def upload(self, key: str, stream: BinaryIO) -> None: ...


class _Pipe:
    """Bounded in-memory pipe; backpressure keeps dump buffering below 1 MiB."""

    def __init__(self) -> None:
        self._queue: queue.Queue[bytes | object] = queue.Queue(maxsize=8)
        self._remainder = b""
        self._ended = False
        self._closed = threading.Event()
        self._error: Exception | None = None

    def write(self, data: bytes) -> int:
        if data:
            while not self._closed.is_set():
                try:
                    self._queue.put(data, timeout=0.1)
                    return len(data)
                except queue.Full:
                    continue
            raise RuntimeError("database dump stream is closed")
        return len(data)

    def flush(self) -> None:
        return None

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        # A producer can fail after placing _END in the queue (or while the
        # reader is waiting for it). Never turn that failed stream into EOF.
        if self._error is not None:
            raise RuntimeError("database dump failed") from self._error
        if size < 0:
            chunks = [self._remainder]
            self._remainder = b""
            while part := self.read(64 * 1024):
                chunks.append(part)
            return b"".join(chunks)
        while len(self._remainder) < size and not self._ended:
            if self._error is not None:
                raise RuntimeError("database dump failed") from self._error
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                if self._closed.is_set() and self._queue.empty():
                    self._ended = True
                continue
            if item is _END:
                self._ended = True
            elif isinstance(item, bytes):
                self._remainder += item
        if self._error is not None:
            raise RuntimeError("database dump failed") from self._error
        result, self._remainder = self._remainder[:size], self._remainder[size:]
        return result

    def finish(self) -> None:
        self._closed.set()
        self._signal_end()

    def fail(self, error: Exception) -> None:
        self._error = error
        self._closed.set()
        self._signal_end()

    def _signal_end(self) -> None:
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(_END)


@dataclass(frozen=True, slots=True)
class DumpRequest:
    engine: DatabaseEngine
    image: str
    network: str
    host: str
    port: int
    database: str
    username: str
    password: str
    key: str


class DumpRunner:
    """Streams dump container stdout through optional gzip and directly to S3 multipart."""

    def __init__(self, runtime: DumpRuntime, store: UploadStore, timeout: float = 21600) -> None:
        self._runtime = runtime
        self._store = store
        self._timeout = timeout

    def run(self, request: DumpRequest) -> int:  # noqa: PLR0915
        if not self._runtime.image_exists(request.image):
            raise BackupError("dump image is not available; check the configured image")
        pipe = _Pipe()
        outcome: list[ExecResult | Exception] = []

        def produce() -> None:
            compressor = (
                gzip.GzipFile(fileobj=pipe, mode="wb")
                if request.engine is not DatabaseEngine.POSTGRESQL
                else None
            )
            try:
                spec = self._spec(request)

                def consume(chunk: bytes) -> None:
                    if compressor is not None:
                        compressor.write(chunk)
                    else:
                        pipe.write(chunk)

                result = self._runtime.run_container_binary(spec, consume, timeout=self._timeout)
                if not result.ok:
                    if result.timed_out:
                        raise BackupError("database dump timed out")
                    if result.cancelled:
                        raise BackupError("database dump was cancelled")
                    raise BackupError(f"dump container exited with code {result.exit_code}")
                if compressor is not None:
                    compressor.close()
                pipe.finish()
                outcome.append(result)
            except Exception as producer_error:
                outcome.append(producer_error)
                pipe.fail(producer_error)

        producer = threading.Thread(target=produce, name="database-dump", daemon=True)
        producer.start()
        try:
            self._store.upload(request.key, pipe)  # type: ignore[arg-type]
        except Exception as upload_error:
            pipe.fail(upload_error)
            if outcome and isinstance(outcome[0], BackupError):
                producer.join(5)
                raise outcome[0] from None
            self._runtime.kill_by_label("backend.database-backup=1")
            producer.join(5)
            raise BackupError("S3 upload failed") from upload_error
        producer.join(self._timeout + 5)
        if producer.is_alive():
            self._runtime.kill_by_label("backend.database-backup=1")
            raise BackupError("database dump timed out")
        if outcome and isinstance(outcome[0], Exception):
            dump_error = outcome[0]
            if isinstance(dump_error, BackupError):
                raise dump_error
            raise RuntimeError("database dump failed") from dump_error
        result = outcome[0] if outcome else None
        if not isinstance(result, ExecResult) or not result.ok:
            raise BackupError("database dump could not be completed")
        return 0

    def _spec(self, request: DumpRequest) -> RunSpec:
        is_mysql = request.engine is not DatabaseEngine.POSTGRESQL
        command = (
            (
                "mysqldump",
                "--single-transaction",
                "--routines",
                "--triggers",
                "-h",
                request.host,
                "-P",
                str(request.port),
                "-u",
                request.username,
                request.database,
            )
            if request.engine is DatabaseEngine.MYSQL
            else (
                (
                    "mariadb-dump",
                    "--single-transaction",
                    "--routines",
                    "--triggers",
                    "-h",
                    request.host,
                    "-P",
                    str(request.port),
                    "-u",
                    request.username,
                    request.database,
                )
                if request.engine is DatabaseEngine.MARIADB
                else (
                    "pg_dump",
                    "-Fc",
                    "-h",
                    request.host,
                    "-p",
                    str(request.port),
                    "-U",
                    request.username,
                    "-d",
                    request.database,
                )
            )
        )
        return RunSpec(
            image=request.image,
            name="jendock-db-backup-" + secrets.token_hex(6),
            command=command,
            env={"MYSQL_PWD" if is_mysql else "PGPASSWORD": request.password},
            network=request.network,
            labels={"backend.database-backup": "1"},
            cpus=0.5,
            memory="512m",
            user="65534:65534",
            hardened=True,
        )
