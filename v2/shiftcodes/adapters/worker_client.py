"""Typed client for the worker RPC socket, used by the API.

Opens one short-lived connection per call. All transport failures become
``WorkerUnavailableError``; worker-reported errors map back to domain errors.
"""

from __future__ import annotations

import json
import secrets
import socket
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError as PydanticValidationError

from shiftcodes.adapters.rpc_protocol import (
    MAX_MESSAGE_BYTES,
    RpcActor,
    RpcRequest,
    RpcResponse,
    domain_error_for,
)
from shiftcodes.domain.actor import Actor
from shiftcodes.domain.enums import EnvironmentName
from shiftcodes.domain.errors import ValidationError, WorkerError, WorkerUnavailableError
from shiftcodes.services.secrets import SecretInfo

DEFAULT_TIMEOUT = 5.0
_CHUNK = 65536


class SecretsGateway(Protocol):
    """Secret operations the API needs; implemented by ``WorkerClient`` and test fakes."""

    def ping(self) -> None:
        """Raise if the worker is unreachable."""

    def set_secret(
        self, slug: str, env: EnvironmentName, name: str, value: str, actor: Actor
    ) -> SecretInfo:
        """Create or rotate a secret."""

    def list_secrets(self, slug: str, env: EnvironmentName, actor: Actor) -> Sequence[SecretInfo]:
        """Return secret metadata."""

    def delete_secret(self, slug: str, env: EnvironmentName, name: str, actor: Actor) -> None:
        """Delete a secret."""

    def reveal_secret(self, slug: str, env: EnvironmentName, name: str, actor: Actor) -> str:
        """Return the plaintext of a secret (audited by the worker)."""


class WorkerClient:
    """Calls the worker over its Unix socket."""

    def __init__(self, socket_path: Path, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._path = socket_path
        self._timeout = timeout

    def ping(self) -> None:
        """Raise ``WorkerUnavailableError`` if the worker does not answer."""
        self._call("ping", None, {})

    def set_secret(
        self, slug: str, env: EnvironmentName, name: str, value: str, actor: Actor
    ) -> SecretInfo:
        """Create or rotate a secret in the worker."""
        params = {"slug": slug, "env": env.value, "name": name, "value": value}
        return _secret_info(self._call("secrets.set", actor, params))

    def list_secrets(self, slug: str, env: EnvironmentName, actor: Actor) -> Sequence[SecretInfo]:
        """Return secret metadata from the worker."""
        result = self._call("secrets.list", actor, {"slug": slug, "env": env.value})
        if not isinstance(result, list):
            raise WorkerError("worker returned an unexpected response")
        return [_secret_info(item) for item in result]

    def delete_secret(self, slug: str, env: EnvironmentName, name: str, actor: Actor) -> None:
        """Delete a secret in the worker."""
        self._call("secrets.delete", actor, {"slug": slug, "env": env.value, "name": name})

    def reveal_secret(self, slug: str, env: EnvironmentName, name: str, actor: Actor) -> str:
        """Return the plaintext of a secret; the worker audits the reveal."""
        params = {"slug": slug, "env": env.value, "name": name}
        result = self._call("secrets.reveal", actor, params)
        value = result.get("value") if isinstance(result, dict) else None
        if not isinstance(value, str):
            raise WorkerError("worker returned an unexpected response")
        return value

    def _call(
        self,
        method: str,
        actor: Actor | None,
        params: Mapping[str, object],
        timeout: float | None = None,
    ) -> object:
        request = RpcRequest(
            id=secrets.token_hex(8),
            method=method,
            actor=RpcActor.from_actor(actor) if actor else None,
            params=dict(params),
        )
        payload = request.model_dump_json().encode() + b"\n"
        if len(payload) > MAX_MESSAGE_BYTES:
            raise ValidationError("request too large")
        response = self._decode(self._exchange(payload, timeout or self._timeout))
        if response.id != request.id:
            raise WorkerError("worker answered a different request")
        if not response.ok or response.error is not None:
            if response.error is None:
                raise WorkerError("worker reported a failure")
            raise domain_error_for(response.error)
        return response.result

    def _exchange(self, payload: bytes, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(timeout)
                connection.connect(str(self._path))
                connection.sendall(payload)
                return _read_line(connection, deadline)
        except (OSError, TimeoutError) as error:
            raise WorkerUnavailableError("worker is not reachable") from error

    @staticmethod
    def _decode(line: bytes) -> RpcResponse:
        try:
            return RpcResponse.model_validate(json.loads(line))
        except (ValueError, PydanticValidationError) as error:
            raise WorkerError("worker returned a malformed response") from error


def _read_line(connection: socket.socket, deadline: float) -> bytes:
    buffer = b""
    while b"\n" not in buffer:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("worker response timed out")
        connection.settimeout(remaining)
        chunk = connection.recv(_CHUNK)
        if not chunk:
            raise ConnectionError("worker closed the connection")
        buffer += chunk
        if len(buffer) > MAX_MESSAGE_BYTES + 1:
            raise ConnectionError("worker response too large")
    return buffer.split(b"\n", 1)[0]


def _secret_info(item: object) -> SecretInfo:
    if not isinstance(item, dict):
        raise WorkerError("worker returned an unexpected response")
    try:
        return SecretInfo(
            name=str(item["name"]),
            created_at=datetime.fromisoformat(str(item["created_at"])),
            updated_at=datetime.fromisoformat(str(item["updated_at"])),
        )
    except (KeyError, ValueError) as error:
        raise WorkerError("worker returned an unexpected response") from error
