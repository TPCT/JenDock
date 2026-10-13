"""The helper's Unix socket server: JSON lines, peer credentials checked, strictly serial.

Request ``{"id": "...", "op": "vhost.put", "params": {...}}``; response
``{"id": "...", "ok": true, "result": {...}}`` or
``{"id": "...", "ok": false, "error": {"code": "...", "message": "..."}}``.
One connection is served at a time, so Apache changes can never interleave.
"""

from __future__ import annotations

import grp
import json
import logging
import os
import socket
import socketserver
import stat
import struct
from pathlib import Path

from shiftcodes.hosthelper.config import HelperConfig
from shiftcodes.hosthelper.errors import HelperError
from shiftcodes.hosthelper.service import HelperService

logger = logging.getLogger("shiftcodes.hosthelper")
MAX_REQUEST_BYTES = 512 * 1024
READ_TIMEOUT_SECONDS = 10.0
PEER_CRED = struct.Struct("3i")
MAX_ID_LENGTH = 64
GENERIC_ERROR = "internal helper error"


def peer_uid(connection: socket.socket) -> int:
    """The uid of the process on the other end, as reported by the kernel."""
    raw = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, PEER_CRED.size)
    _pid, uid, _gid = PEER_CRED.unpack(raw)
    return int(uid)


def failure(request_id: str, code: str, message: str) -> dict[str, object]:
    """An error response."""
    return {"id": request_id, "ok": False, "error": {"code": code, "message": message}}


def handle_line(service: HelperService, line: bytes) -> dict[str, object]:
    """Turn one request line into a response; never raises."""
    try:
        request = json.loads(line)
    except ValueError:
        return failure("", "invalid_request", "malformed request")
    request_id = request.get("id") if isinstance(request, dict) else None
    if not isinstance(request, dict) or not isinstance(request_id, str):
        return failure("", "invalid_request", "malformed request")
    if len(request_id) > MAX_ID_LENGTH or set(request) - {"id", "op", "params"}:
        return failure("", "invalid_request", "malformed request")
    try:
        result = service.execute(request.get("op"), request.get("params", {}))
    except HelperError as error:
        return failure(request_id, error.code, error.message)
    except Exception:
        logger.exception("operation failed")
        return failure(request_id, "internal", GENERIC_ERROR)
    return {"id": request_id, "ok": True, "result": result}


class _Handler(socketserver.StreamRequestHandler):
    server: HelperServer

    def handle(self) -> None:
        self.request.settimeout(READ_TIMEOUT_SECONDS)
        try:
            if peer_uid(self.request) not in self.server.config.allowed_uids:
                self._send(failure("", "forbidden", "peer is not allowed"))
                return
            line = self.rfile.readline(MAX_REQUEST_BYTES + 1)
            if not line:
                return
            if len(line) > MAX_REQUEST_BYTES:
                self._send(failure("", "invalid_request", "request too large"))
                return
            self._send(handle_line(self.server.service, line))
        except OSError:
            logger.debug("connection closed")

    def _send(self, response: dict[str, object]) -> None:
        self.wfile.write(json.dumps(response).encode() + b"\n")
        self.wfile.flush()


class HelperServer(socketserver.UnixStreamServer):
    """Binds the socket with the configured mode and group."""

    def __init__(self, config: HelperConfig, service: HelperService) -> None:
        self.config = config
        self.service = service
        _prepare_socket_path(config.socket_path)
        previous = os.umask(0o177)
        try:
            super().__init__(str(config.socket_path), _Handler)
        finally:
            os.umask(previous)
        self._apply_permissions()

    def _apply_permissions(self) -> None:
        path = self.config.socket_path
        if self.config.socket_group is not None:
            os.chown(path, -1, grp.getgrnam(self.config.socket_group).gr_gid)
        path.chmod(self.config.socket_mode)

    def server_close(self) -> None:
        """Close and remove the socket file this process created."""
        super().server_close()
        self.config.socket_path.unlink(missing_ok=True)


def _prepare_socket_path(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(mode):
        raise FileExistsError(f"{path} exists and is not a socket")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.connect(str(path))
    except OSError:
        path.unlink()  # stale socket of a crashed helper
        return
    finally:
        probe.close()
    raise FileExistsError(f"another helper is already listening on {path}")
