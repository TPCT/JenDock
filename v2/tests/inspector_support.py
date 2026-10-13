"""Fakes for the inspector's platform side: a scripted helper and a worker that serves ``db.*``."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Engine

from shiftcodes.adapters.inspector_runner import HelperRequest
from shiftcodes.domain.errors import DomainError
from shiftcodes.inspector.model import InspectorError
from shiftcodes.repositories.database import UnitOfWork, create_session_factory
from shiftcodes.services.database_inspection import HelperPort, InspectionService
from shiftcodes.services.secret_cipher import SecretCipher
from shiftcodes.services.secrets import SecretService
from shiftcodes.worker.database_handlers import build_database_handlers
from shiftcodes.worker.handlers import build_handlers
from shiftcodes.worker.rpc import RpcServer

Responder = Callable[[HelperRequest], dict[str, object]]
SERVER = {
    "version": "PostgreSQL 16.4",
    "read_only": True,
    "current_user": "reader",
    "database": "app",
    "tls": False,
}
TABLE = {"schema": "public", "name": "users", "kind": "table", "approx_rows": 42}
PAGE = {
    "columns": [{"name": "id", "type": "integer"}, {"name": "email", "type": "text"}],
    "rows": [[1, "a@example.test"], [2, "b@example.test"]],
    "limit": 2,
    "offset": 0,
    "ordering": "primary_key",
    "order_by": ["id"],
    "next_after": [2],
    "next_offset": 2,
    "size_limited": False,
}
QUERY = {
    "columns": [{"name": "n", "type": "int4"}],
    "rows": [[7777]],
    "row_count": 1,
    "truncated": False,
    "size_limited": False,
    "limit": 500,
    "elapsed_ms": 3,
    "statement_kind": "SELECT",
}
DETAIL: dict[str, object] = {
    "table": TABLE,
    "columns": [
        {
            "name": "id",
            "position": 1,
            "data_type": "integer",
            "nullable": False,
            "default": None,
            "primary_key_position": 1,
            "extra": None,
        }
    ],
    "indexes": [
        {
            "name": "users_pkey",
            "columns": ["id"],
            "unique": True,
            "primary": True,
            "method": "btree",
        }
    ],
    "foreign_keys": [],
}
DEFAULTS: dict[str, dict[str, object]] = {
    "test": SERVER,
    "schemas": {"schemas": [{"name": "public", "system": False}]},
    "tables": {"tables": [TABLE]},
    "table": DETAIL,
    "rows": PAGE,
    "query": QUERY,
}


@dataclass(slots=True)
class FakeHelper:
    """Records requests and answers from ``DEFAULTS`` unless a responder or error is set."""

    requests: list[tuple[str, HelperRequest]] = field(default_factory=list)
    responders: dict[str, Responder] = field(default_factory=dict)
    failure: InspectorError | DomainError | None = None

    def run(self, network: str, request: HelperRequest) -> dict[str, object]:
        """Record the call and answer it."""
        self.requests.append((network, request))
        if self.failure is not None:
            raise self.failure
        responder = self.responders.get(request.operation)
        if responder is not None:
            return responder(request)
        return dict(DEFAULTS[request.operation])


@contextmanager
def running_inspection_worker(
    socket_dir: Path, engine: Engine, key_file: Path, helper: HelperPort
) -> Iterator[Path]:
    """A real RPC server with secrets and ``db.*`` handlers; the helper container is faked."""
    socket_path = socket_dir / "w.sock"
    cipher = SecretCipher(key_file)
    session_factory = create_session_factory(engine)

    def secret_service() -> SecretService:
        return SecretService(UnitOfWork(session_factory), cipher)

    def inspection() -> InspectionService:
        return InspectionService(lambda: UnitOfWork(session_factory), secret_service, helper)

    handlers = build_handlers(secret_service, build_database_handlers(inspection))
    server = RpcServer(socket_path, handlers)
    server.start()
    try:
        yield socket_path
    finally:
        server.stop()
