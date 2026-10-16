"""The API's view of the database inspector: ``db.*`` calls to the worker.

Results are plain JSON (dicts and lists) exactly as the worker's helper produced them; the API
schemas validate their shape before they reach a client.
"""

from __future__ import annotations

from typing import Protocol

from backend.adapters.worker_client import WorkerClient
from backend.domain.actor import Actor
from backend.domain.database import ConnectionRef, TableRef
from backend.domain.enums import EnvironmentName
from backend.domain.errors import WorkerError

# The helper container is allowed 30 s; leave headroom for starting it.
INSPECT_TIMEOUT = 45.0
DETECT_TIMEOUT = 180.0
Json = dict[str, object]


class InspectorGateway(Protocol):
    """Inspector operations the API needs; implemented by ``WorkerInspectorClient``."""

    def test(self, ref: ConnectionRef, actor: Actor) -> Json:
        """Connect through the environment network and report."""

    def schemas(self, ref: ConnectionRef, actor: Actor, *, include_system: bool) -> Json:
        """List schemas."""

    def tables(self, ref: ConnectionRef, schema: str, actor: Actor) -> Json:
        """List tables of a schema."""

    def table(self, ref: TableRef, actor: Actor) -> Json:
        """Columns, indexes and foreign keys of a table."""

    def rows(self, ref: TableRef, page: Json, actor: Actor) -> Json:
        """One page of rows."""

    def query(self, ref: ConnectionRef, statement: str, actor: Actor) -> Json:
        """Run one read-only console statement."""

    def detect(self, slug: str, env: EnvironmentName, actor: Actor, *, prune: bool = False) -> Json:
        """Detect connections inside the worker; return safe metadata only."""


class WorkerInspectorClient:
    """Implements ``InspectorGateway`` over the worker RPC socket."""

    def __init__(self, client: WorkerClient) -> None:
        self._client = client

    def test(self, ref: ConnectionRef, actor: Actor) -> Json:
        """``db.test``."""
        return self._call("db.test", actor, ref, {})

    def schemas(self, ref: ConnectionRef, actor: Actor, *, include_system: bool) -> Json:
        """``db.schemas``."""
        return self._call("db.schemas", actor, ref, {"include_system": include_system})

    def tables(self, ref: ConnectionRef, schema: str, actor: Actor) -> Json:
        """``db.tables``."""
        return self._call("db.tables", actor, ref, {"schema": schema})

    def table(self, ref: TableRef, actor: Actor) -> Json:
        """``db.table``."""
        params: Json = {"schema": ref.schema, "table": ref.table}
        return self._call("db.table", actor, ref.connection, params)

    def rows(self, ref: TableRef, page: Json, actor: Actor) -> Json:
        """``db.rows``."""
        params: Json = {"schema": ref.schema, "table": ref.table, **page}
        return self._call("db.rows", actor, ref.connection, params)

    def query(self, ref: ConnectionRef, statement: str, actor: Actor) -> Json:
        """``db.query``."""
        return self._call("db.query", actor, ref, {"statement": statement})

    def detect(self, slug: str, env: EnvironmentName, actor: Actor, *, prune: bool = False) -> Json:
        result = self._client.call(
            "db.detect",
            actor,
            {"slug": slug, "env": env.value, "prune": prune},
            timeout=DETECT_TIMEOUT,
        )
        if not isinstance(result, dict):
            raise WorkerError("worker returned an unexpected response")
        return result

    def _call(self, method: str, actor: Actor, ref: ConnectionRef, params: Json) -> Json:
        body: Json = {"slug": ref.slug, "env": ref.env.value, "name": ref.name, **params}
        result = self._client.call(method, actor, body, timeout=INSPECT_TIMEOUT)
        if not isinstance(result, dict):
            raise WorkerError("worker returned an unexpected response")
        return result
