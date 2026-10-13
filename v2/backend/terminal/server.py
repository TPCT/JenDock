"""The worker's terminal socket: one connection per browser terminal, framed binary protocol.

Like the RPC socket it is a Unix socket with mode 0660 in the shared socket directory, so only
the API (same group) can connect. Authorization of *users* happened in the API (ticket flow);
the worker still checks that the container belongs to the environment and is running.
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import socketserver
import stat
import threading
import time
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from backend.adapters.rpc_protocol import RpcActor
from backend.domain.containers import validate_container_id
from backend.domain.enums import EnvironmentName
from backend.domain.errors import DomainError
from backend.terminal.protocol import (
    CLOSE_REASONS,
    MAX_DATA_BYTES,
    REASON_CLOSED,
    Frame,
    FrameDecoder,
    FrameType,
    ProtocolError,
    SequenceCounter,
    decode_data,
    decode_json,
    encode_data,
    encode_json,
)
from backend.terminal.pty_process import clamp_size
from backend.terminal.session import (
    InputLimiter,
    OpenSpec,
    TerminalManager,
    TerminalSession,
)

logger = logging.getLogger(__name__)
SOCKET_MODE = 0o660
OPEN_TIMEOUT = 5.0
SEND_TIMEOUT = 10.0
RECV_CHUNK = 65536
DEFAULT_ROWS = 24
DEFAULT_COLS = 80


class OpenRequest(BaseModel):
    """The first frame: which container to open a terminal in, or which terminal to reattach."""

    model_config = ConfigDict(extra="forbid")

    slug: str = Field(max_length=64)
    env: EnvironmentName
    container_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    actor: RpcActor
    rows: int = DEFAULT_ROWS
    cols: int = DEFAULT_COLS
    attach: str | None = Field(default=None, max_length=64)


class TerminalOpener(Protocol):
    """Authorizes a container and builds the command (``docker exec -it <id> /bin/sh``)."""

    def prepare(self, request: OpenRequest) -> OpenSpec:
        """Return the spec or raise a ``DomainError`` (not found, not running, ...)."""


class SocketSink:
    """The worker's end of one browser terminal connection."""

    def __init__(self, connection: socket.socket) -> None:
        self._connection = connection
        self._lock = threading.Lock()
        self._sequence = SequenceCounter()
        self._alive = True

    def send_control(self, frame_type: FrameType, body: dict[str, object]) -> bool:
        """Send a JSON frame."""
        return self._send(encode_json(frame_type, body))

    def send_output(self, data: bytes) -> bool:
        """Send terminal output in bounded, sequenced frames."""
        with self._lock:
            if not self._alive:
                return False
            try:
                for start in range(0, len(data), MAX_DATA_BYTES):
                    chunk = data[start : start + MAX_DATA_BYTES]
                    self._connection.sendall(encode_data(self._sequence.take(), chunk))
            except OSError:
                self._alive = False
        return self._alive

    def send_exit(self, code: int | None, reason: str) -> None:
        """Tell the API that the terminal ended."""
        self.send_control(FrameType.EXIT, {"code": code, "reason": reason})

    def close(self) -> None:
        """Close the socket (wakes the reading thread)."""
        with self._lock:
            self._alive = False
        with contextlib.suppress(OSError):
            self._connection.shutdown(socket.SHUT_RDWR)
        with contextlib.suppress(OSError):
            self._connection.close()

    def _send(self, payload: bytes) -> bool:
        with self._lock:
            if not self._alive:
                return False
            try:
                self._connection.sendall(payload)
            except OSError:
                self._alive = False
        return self._alive


class _Connection:
    """Serves one accepted socket."""

    def __init__(self, connection: socket.socket, manager: TerminalManager, opener: TerminalOpener):
        self._connection = connection
        self._manager = manager
        self._opener = opener
        self._decoder = FrameDecoder()
        self._sink = SocketSink(connection)
        self._input = SequenceCounter()
        self._limiter = InputLimiter()
        self._session: TerminalSession | None = None
        self._pending: list[Frame] = []

    def serve(self) -> None:
        """Open or reattach, then relay input until the connection ends."""
        # One timeout for both directions: a stalled peer cannot block output delivery forever.
        self._connection.settimeout(SEND_TIMEOUT)
        try:
            self._open()
            self._relay()
        except (ProtocolError, DomainError, OSError, PydanticValidationError) as error:
            self._fail(error)
        finally:
            if self._session is not None:
                self._session.detach(self._sink)
            self._sink.close()

    def _read_frames(self) -> list[Frame] | None:
        """Frames of the next chunk; ``[]`` after a quiet period, ``None`` when the peer closed."""
        try:
            data = self._connection.recv(RECV_CHUNK)
        except TimeoutError:
            return []
        return self._decoder.feed(data) if data else None

    def _open(self) -> None:
        deadline = time.monotonic() + OPEN_TIMEOUT
        frames: list[Frame] | None = []
        while not frames:
            frames = self._read_frames()
            if frames is None:
                raise ProtocolError("closed before open")
            if not frames and time.monotonic() > deadline:
                raise ProtocolError("open timed out")
        first, rest = frames[0], frames[1:]
        if first.type is not FrameType.OPEN:
            raise ProtocolError("expected an open frame")
        request = OpenRequest.model_validate(decode_json(first))
        validate_container_id(request.container_id)
        self._pending = rest
        actor = request.actor.to_actor()
        if request.attach:
            session = self._manager.lookup(request.attach, actor)
            self._announce(session.id)
            self._manager.reattach(request.attach, actor, self._sink)
        else:
            spec = self._opener.prepare(request)
            session = self._manager.create(spec)
            try:
                self._announce(session.id)
            except OSError:
                self._manager.discard(session)
                raise
            self._manager.begin(session, self._sink)
        self._session = session

    def _announce(self, terminal_id: str) -> None:
        if not self._sink.send_control(FrameType.READY, {"terminal_id": terminal_id}):
            raise OSError("client went away")

    def _relay(self) -> None:
        session = self._session
        if session is None:
            return
        self._handle(session, self._pending)
        while True:
            frames = self._read_frames()
            if frames is None:
                return
            self._handle(session, frames)

    def _handle(self, session: TerminalSession, frames: list[Frame]) -> None:
        for frame in frames:
            if frame.type is FrameType.DATA:
                sequence, data = decode_data(frame)
                self._input.expect(sequence)
                self._limiter.wait(len(data))
                session.write(data)
            elif frame.type is FrameType.RESIZE:
                body = decode_json(frame)
                rows, cols = body.get("rows"), body.get("cols")
                if isinstance(rows, int) and isinstance(cols, int):
                    session.resize(*clamp_size(rows, cols))
            elif frame.type is FrameType.CLOSE:
                session.finish(_close_reason(frame))
            else:
                raise ProtocolError("unexpected frame")

    def _fail(self, error: Exception) -> None:
        message = (
            str(error) if isinstance(error, DomainError | ProtocolError) else "terminal failed"
        )
        if isinstance(error, PydanticValidationError):
            message = "invalid request"
        self._sink.send_control(FrameType.ERROR, {"message": message[:300]})


def _close_reason(frame: Frame) -> str:
    """The audited reason of a CLOSE frame; anything unknown counts as a plain close."""
    if not frame.payload:
        return REASON_CLOSED
    reason = decode_json(frame).get("reason")
    return reason if isinstance(reason, str) and reason in CLOSE_REASONS else REASON_CLOSED


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: Path, manager: TerminalManager, opener: TerminalOpener) -> None:
        self.manager = manager
        self.opener = opener
        super().__init__(str(path), _Handler)


class _Handler(socketserver.BaseRequestHandler):
    server: _UnixServer

    def handle(self) -> None:
        connection: socket.socket = self.request
        _Connection(connection, self.server.manager, self.server.opener).serve()


class TerminalServer:
    """Owns the terminal socket file and its serving thread."""

    def __init__(self, socket_path: Path, manager: TerminalManager, opener: TerminalOpener) -> None:
        self._path = socket_path
        self._manager = manager
        self._opener = opener
        self._server: _UnixServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Bind the socket (mode 0660) and serve in a background thread."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        _remove_stale_socket(self._path)
        previous = os.umask(0o117)
        try:
            self._server = _UnixServer(self._path, self._manager, self._opener)
        finally:
            os.umask(previous)
        self._path.chmod(SOCKET_MODE)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.05},
            name="terminal-rpc",
            daemon=True,
        )
        self._thread.start()
        logger.info("terminal server listening", extra={"socket": str(self._path)})

    def stop(self) -> None:
        """Stop accepting, close the socket and remove its file (sessions end separately)."""
        server, thread = self._server, self._thread
        if server is None or thread is None:
            return
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        self._path.unlink(missing_ok=True)
        self._server = None
        self._thread = None


def _remove_stale_socket(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(mode):
        raise FileExistsError(f"{path} exists and is not a socket")
    path.unlink()
