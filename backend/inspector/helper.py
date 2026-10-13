"""Entrypoint of the short-lived helper container: ``python -m backend.inspector.helper``.

The worker starts it attached to the environment's compose network. The request (operation,
arguments, connection including the database password, and the network policy) arrives in the
``JENDOCK_INSPECT_REQUEST`` environment variable and is removed from the environment before
anything runs. The answer is written to stdout in the framing of ``wire``. Exit code 0 means an
answer was written (it may describe a failure); anything else is an internal crash.
"""

from __future__ import annotations

import json
import os
import sys

from backend.inspector import wire
from backend.inspector.model import ConnectionSpec, Engine, ErrorCode, InspectorError, TlsMode
from backend.inspector.netguard import NetworkPolicy
from backend.inspector.operations import run_operation

REQUEST_ENV = "JENDOCK_INSPECT_REQUEST"
MAX_PORT = 65535


def _field[T](source: object, key: str, kind: type[T]) -> T:
    value = source.get(key) if isinstance(source, dict) else None
    if not isinstance(value, kind) or (isinstance(value, bool) and kind is not bool):
        raise InspectorError(ErrorCode.BAD_REQUEST, f"invalid request field: {key}")
    return value


def parse_request(
    raw: str,
) -> tuple[str, dict[str, object], ConnectionSpec, NetworkPolicy]:
    """Validate the request JSON into its parts."""
    try:
        request = json.loads(raw)
    except ValueError as error:
        raise InspectorError(ErrorCode.BAD_REQUEST, "request is not JSON") from error
    operation = _field(request, "op", str)
    args = _field(request, "args", dict)
    connection = _field(request, "connection", dict)
    network = _field(request, "network", dict)
    port = _field(connection, "port", int)
    if not 1 <= port <= MAX_PORT:
        raise InspectorError(ErrorCode.BAD_REQUEST, "invalid port")
    try:
        spec = ConnectionSpec(
            engine=Engine(_field(connection, "engine", str)),
            host=_field(connection, "host", str),
            port=port,
            database=_field(connection, "database", str),
            username=_field(connection, "username", str),
            password=_field(connection, "password", str),
            tls=TlsMode(_field(connection, "tls", str)),
        )
    except ValueError as error:
        raise InspectorError(ErrorCode.BAD_REQUEST, "invalid connection") from error
    subnets = tuple(str(item) for item in _field(network, "subnets", list))
    blocked = tuple(str(item) for item in _field(network, "blocked", list))
    return operation, args, spec, NetworkPolicy(subnets, blocked)


def handle(raw: str) -> dict[str, object]:
    """Run the request and return the answer payload (never raises for inspector errors)."""
    try:
        operation, args, spec, policy = parse_request(raw)
        return {"ok": True, "result": run_operation(operation, args, spec, policy)}
    except InspectorError as error:
        return {"ok": False, "error": {"code": error.code.value, "message": error.message}}


def main() -> int:
    """Read the request from the environment, answer on stdout."""
    raw = os.environ.pop(REQUEST_ENV, "")
    if not raw:
        sys.stderr.write("no request\n")
        return 2
    answer = handle(raw)
    for line in wire.encode(answer):
        sys.stdout.write(line + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
