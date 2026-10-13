"""JSON-lines RPC server on a Unix socket, run by the worker.

The socket is created with mode 0660 so only the worker user and the shared group (the
API) can connect. Only methods registered up front are reachable; requests are size
limited; unexpected failures return a generic error and never leak exception text.
"""

from __future__ import annotations

import json
import logging
import os
import socketserver
import stat
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError as PydanticValidationError

from backend.adapters.rpc_protocol import (
    MAX_MESSAGE_BYTES,
    ErrorCode,
    RpcErrorBody,
    RpcRequest,
    RpcResponse,
    error_code_for,
)
from backend.domain.actor import Actor
from backend.domain.errors import DomainError

logger = logging.getLogger(__name__)
SOCKET_MODE = 0o660
POLL_INTERVAL = 0.05
DEFAULT_READ_TIMEOUT = 10.0
GENERIC_INTERNAL_MESSAGE = "internal worker error"


class BadRequestError(Exception):
    """A handler rejected malformed params or a missing actor."""


@dataclass(frozen=True, slots=True)
class RpcCall:
    """What a handler receives: the acting principal and raw params."""

    actor: Actor | None
    params: Mapping[str, object]

    def require_actor(self) -> Actor:
        """Return the actor or raise ``BadRequestError``."""
        if self.actor is None:
            raise BadRequestError("actor is required")
        return self.actor


Handler = Callable[[RpcCall], object]


class RpcDispatcher:
    """Decodes request lines, calls allowlisted handlers, encodes responses."""

    def __init__(self, handlers: Mapping[str, Handler]) -> None:
        self._handlers = dict(handlers)

    def handle_line(self, line: bytes) -> RpcResponse:
        """Turn one request line into a response; never raises."""
        try:
            request = RpcRequest.model_validate(json.loads(line))
        except (ValueError, PydanticValidationError):
            return _failure("", ErrorCode.BAD_REQUEST, "malformed request")
        handler = self._handlers.get(request.method)
        if handler is None:
            return _failure(request.id, ErrorCode.UNKNOWN_METHOD, "unknown method")
        actor = request.actor.to_actor() if request.actor else None
        return self._invoke(request, handler, RpcCall(actor, request.params))

    def _invoke(self, request: RpcRequest, handler: Handler, call: RpcCall) -> RpcResponse:
        try:
            return RpcResponse(id=request.id, ok=True, result=handler(call))
        except BadRequestError as error:
            return _failure(request.id, ErrorCode.BAD_REQUEST, str(error))
        except DomainError as error:
            return _failure(request.id, error_code_for(error), str(error))
        except Exception:
            logger.exception("rpc handler failed", extra={"method": request.method})
            return _failure(request.id, ErrorCode.INTERNAL, GENERIC_INTERNAL_MESSAGE)


def _failure(request_id: str, code: ErrorCode, message: str) -> RpcResponse:
    return RpcResponse(id=request_id, ok=False, error=RpcErrorBody(code=code, message=message))


class _Handler(socketserver.StreamRequestHandler):
    server: _UnixServer

    def handle(self) -> None:
        self.request.settimeout(self.server.read_timeout)
        try:
            while self._serve_one():
                pass
        except OSError:
            logger.debug("rpc connection closed")

    def _serve_one(self) -> bool:
        line = self.rfile.readline(MAX_MESSAGE_BYTES + 1)
        if not line:
            return False
        if len(line) > MAX_MESSAGE_BYTES:
            self._send(_failure("", ErrorCode.TOO_LARGE, "request too large"))
            return False
        self._send(self.server.dispatcher.handle_line(line))
        return True

    def _send(self, response: RpcResponse) -> None:
        self.wfile.write(response.model_dump_json().encode() + b"\n")
        self.wfile.flush()


class _UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: Path, dispatcher: RpcDispatcher, read_timeout: float) -> None:
        self.dispatcher = dispatcher
        self.read_timeout = read_timeout
        super().__init__(str(path), _Handler)


class RpcServer:
    """Owns the socket file and the serving thread."""

    def __init__(
        self,
        socket_path: Path,
        handlers: Mapping[str, Handler],
        read_timeout: float = DEFAULT_READ_TIMEOUT,
    ) -> None:
        self._path = socket_path
        self._dispatcher = RpcDispatcher(handlers)
        self._read_timeout = read_timeout
        self._server: _UnixServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Bind the socket (mode 0660) and serve in a background thread."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        _remove_stale_socket(self._path)
        previous = os.umask(0o117)
        try:
            self._server = _UnixServer(self._path, self._dispatcher, self._read_timeout)
        finally:
            os.umask(previous)
        self._path.chmod(SOCKET_MODE)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": POLL_INTERVAL},
            name="rpc",
            daemon=True,
        )
        self._thread.start()
        logger.info("rpc server listening", extra={"socket": str(self._path)})

    def stop(self) -> None:
        """Stop accepting, close the socket and remove its file."""
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
