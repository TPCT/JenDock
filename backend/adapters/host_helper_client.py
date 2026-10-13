"""Client of the host helper's Unix socket (used by the worker, never by the API)."""

from __future__ import annotations

import json
import secrets
import socket
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from backend.adapters.errors import AdapterError

DEFAULT_TIMEOUT = 60.0
MAX_RESPONSE_BYTES = 1024 * 1024
_CHUNK = 65536


class HostHelperError(AdapterError):
    """The helper refused a request or could not be reached; ``code`` mirrors the protocol."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class VhostProposal:
    """What the helper would write and what is there now (``None`` when absent)."""

    proposed: str
    current: str | None


@dataclass(frozen=True, slots=True)
class InstalledCertificate:
    """Validity of a certificate the helper installed."""

    not_after: datetime
    serial: str


class HostHelper(Protocol):
    """The helper operations the platform uses; implemented by the client and by test fakes."""

    def render_vhost(
        self, hostname: str, upstream_port: int, *, tls: bool, websocket: bool = True
    ) -> VhostProposal:
        """Render (do not write) the vhost and return it with the current file."""

    def get_vhost(self, hostname: str) -> str | None:
        """Return the managed vhost text of ``hostname`` or ``None``."""

    def put_vhost(
        self, hostname: str, upstream_port: int, *, tls: bool, websocket: bool = True
    ) -> bool:
        """Write the vhost, test and reload Apache; return whether the file changed."""

    def remove_vhost(self, hostname: str) -> bool:
        """Remove the vhost, test and reload; return whether a file was removed."""

    def install_certificate(self, hostname: str, fullchain_pem: str, private_key_pem: str) -> None:
        """Install a certificate (reloads Apache when the vhost already serves TLS)."""

    def certificate_info(self, hostname: str) -> InstalledCertificate | None:
        """Validity of the certificate installed on the host, or ``None``."""

    def remove_certificate(self, hostname: str) -> bool:
        """Remove the installed certificate files."""

    def put_challenge(self, token: str, content: str) -> None:
        """Publish an HTTP-01 challenge file."""

    def remove_challenge(self, token: str) -> None:
        """Remove an HTTP-01 challenge file."""

    def check_backend(self, port: int) -> bool:
        """Whether something accepts TCP connections on ``127.0.0.1:port`` on the host."""


class HostHelperClient:
    """Speaks the helper's JSON-lines protocol; one connection per call."""

    def __init__(self, socket_path: Path, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._path = socket_path
        self._timeout = timeout

    def ping(self) -> None:
        """Raise ``HostHelperError`` if the helper does not answer."""
        self._call("ping", {})

    def render_vhost(
        self, hostname: str, upstream_port: int, *, tls: bool, websocket: bool = True
    ) -> VhostProposal:
        """Ask the helper to render the vhost without writing it."""
        result = self._call("vhost.render", _vhost(hostname, upstream_port, tls, websocket))
        proposed, current = result.get("proposed"), result.get("current")
        if not isinstance(proposed, str) or not (current is None or isinstance(current, str)):
            raise HostHelperError("protocol", "unexpected helper response")
        return VhostProposal(proposed, current)

    def get_vhost(self, hostname: str) -> str | None:
        """Return the managed vhost text of ``hostname`` or ``None``."""
        content = self._call("vhost.get", {"hostname": hostname}).get("content")
        return content if isinstance(content, str) else None

    def put_vhost(
        self, hostname: str, upstream_port: int, *, tls: bool, websocket: bool = True
    ) -> bool:
        """Write the vhost; the helper tests the config, reloads and reverts on failure."""
        result = self._call("vhost.put", _vhost(hostname, upstream_port, tls, websocket))
        return bool(result.get("changed"))

    def remove_vhost(self, hostname: str) -> bool:
        """Remove the vhost."""
        return bool(self._call("vhost.remove", {"hostname": hostname}).get("removed"))

    def install_certificate(self, hostname: str, fullchain_pem: str, private_key_pem: str) -> None:
        """Hand a certificate and its key to the helper."""
        params = {
            "hostname": hostname,
            "fullchain_pem": fullchain_pem,
            "private_key_pem": private_key_pem,
        }
        self._call("cert.install", params)

    def certificate_info(self, hostname: str) -> InstalledCertificate | None:
        """Return the validity of the certificate installed on the host, if any."""
        result = self._call("cert.info", {"hostname": hostname})
        if not result.get("installed"):
            return None
        try:
            return InstalledCertificate(
                datetime.fromisoformat(str(result["not_after"])), str(result["serial"])
            )
        except (KeyError, ValueError) as error:
            raise HostHelperError("protocol", "unexpected helper response") from error

    def remove_certificate(self, hostname: str) -> bool:
        """Remove the certificate files of ``hostname``."""
        return bool(self._call("cert.remove", {"hostname": hostname}).get("removed"))

    def put_challenge(self, token: str, content: str) -> None:
        """Publish an HTTP-01 challenge file."""
        self._call("challenge.put", {"token": token, "content": content})

    def remove_challenge(self, token: str) -> None:
        """Remove an HTTP-01 challenge file."""
        self._call("challenge.remove", {"token": token})

    def check_backend(self, port: int) -> bool:
        """Probe ``127.0.0.1:port`` from the host."""
        return bool(self._call("backend.check", {"port": port}).get("reachable"))

    def _call(self, operation: str, params: Mapping[str, object]) -> Mapping[str, object]:
        request_id = secrets.token_hex(8)
        payload = json.dumps({"id": request_id, "op": operation, "params": dict(params)})
        response = self._exchange(payload.encode() + b"\n")
        if response.get("id") != request_id and response.get("id") != "":
            raise HostHelperError("protocol", "helper answered a different request")
        if response.get("ok") is True:
            result = response.get("result")
            if isinstance(result, dict):
                return result
            raise HostHelperError("protocol", "unexpected helper response")
        error = response.get("error")
        if isinstance(error, dict):
            raise HostHelperError(str(error.get("code", "error")), str(error.get("message", "")))
        raise HostHelperError("protocol", "unexpected helper response")

    def _exchange(self, payload: bytes) -> Mapping[str, object]:
        deadline = time.monotonic() + self._timeout
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self._timeout)
                connection.connect(str(self._path))
                connection.sendall(payload)
                line = _read_line(connection, deadline)
        except (OSError, TimeoutError) as error:
            raise HostHelperError("unavailable", "the host helper is not reachable") from error
        try:
            decoded = json.loads(line)
        except ValueError as error:
            raise HostHelperError("protocol", "malformed helper response") from error
        if not isinstance(decoded, dict):
            raise HostHelperError("protocol", "malformed helper response")
        return decoded


def _vhost(hostname: str, port: int, tls: bool, websocket: bool) -> dict[str, object]:
    return {"hostname": hostname, "upstream_port": port, "tls": tls, "websocket": websocket}


def _read_line(connection: socket.socket, deadline: float) -> bytes:
    buffer = b""
    while b"\n" not in buffer:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("helper response timed out")
        connection.settimeout(remaining)
        chunk = connection.recv(_CHUNK)
        if not chunk:
            raise ConnectionError("helper closed the connection")
        buffer += chunk
        if len(buffer) > MAX_RESPONSE_BYTES:
            raise ConnectionError("helper response too large")
    return buffer.split(b"\n", 1)[0]
