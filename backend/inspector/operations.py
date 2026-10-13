"""The operations of the inspector, dispatched by name (used by the helper and by tests)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Protocol

from backend.inspector.model import (
    CONSOLE_TIMEOUT_MS,
    DEFAULT_PAGE_ROWS,
    METADATA_TIMEOUT_MS,
    ConnectionSpec,
    Engine,
    ErrorCode,
    InspectorError,
    Limits,
    QueryResult,
    RowPage,
    RowRequest,
    SchemaInfo,
    ServerInfo,
    TableDetail,
    TableInfo,
)
from backend.inspector.mysql import MySqlAdapter
from backend.inspector.netguard import NetworkPolicy, resolve_address
from backend.inspector.postgres import PostgresAdapter

OPERATIONS = frozenset({"test", "schemas", "tables", "table", "rows", "query"})
MAX_NAME_CHARS = 256


class InspectorAdapter(Protocol):
    """What every engine adapter offers. All methods are read-only."""

    def close(self) -> None:
        """Roll back and close the session."""

    def server_info(self) -> ServerInfo:
        """Version, user and read-only state."""

    def schemas(self, *, include_system: bool = False) -> list[SchemaInfo]:
        """List schemas."""

    def tables(self, schema: str) -> list[TableInfo]:
        """List tables and views with approximate row counts."""

    def table_detail(self, schema: str, table: str) -> TableDetail:
        """Columns, indexes and foreign keys."""

    def rows(self, schema: str, table: str, request: RowRequest) -> RowPage:
        """One page of rows."""

    def query(self, statement: str) -> QueryResult:
        """Run one validated read-only statement."""


def open_adapter(spec: ConnectionSpec, address: str, limits: Limits) -> InspectorAdapter:
    """Connect to ``address`` (already resolved and checked) with the engine's adapter."""
    if spec.engine is Engine.POSTGRESQL:
        return PostgresAdapter(spec, address, limits)
    return MySqlAdapter(spec, address, limits)


def limits_for(operation: str) -> Limits:
    """The console gets its own, shorter statement timeout."""
    timeout = CONSOLE_TIMEOUT_MS if operation == "query" else METADATA_TIMEOUT_MS
    return Limits(statement_timeout_ms=timeout)


def run_operation(
    operation: str, args: dict[str, object], spec: ConnectionSpec, policy: NetworkPolicy
) -> dict[str, object]:
    """Resolve the host inside the policy, connect, run one operation, close, return JSON."""
    if operation not in OPERATIONS:
        raise InspectorError(ErrorCode.BAD_REQUEST, f"unknown operation: {operation}")
    address = resolve_address(spec.host, spec.port, policy)
    adapter = open_adapter(spec, address, limits_for(operation))
    try:
        return dispatch(adapter, operation, args)
    finally:
        adapter.close()


def _name(args: dict[str, object], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value or len(value) > MAX_NAME_CHARS:
        raise InspectorError(ErrorCode.BAD_REQUEST, f"{key} is required")
    return value


def _statement(args: dict[str, object]) -> str:
    value = args.get("statement")
    if not isinstance(value, str):
        raise InspectorError(ErrorCode.BAD_REQUEST, "statement is required")
    return value


def _int(args: dict[str, object], key: str, default: int) -> int:
    value = args.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise InspectorError(ErrorCode.BAD_REQUEST, f"{key} must be an integer")
    return value


def dispatch(
    adapter: InspectorAdapter, operation: str, args: dict[str, object]
) -> dict[str, object]:
    """Run ``operation`` on an open adapter and return a JSON-ready dict."""
    if operation == "test":
        return asdict(adapter.server_info())
    if operation == "schemas":
        include = args.get("include_system") is True
        return {"schemas": [asdict(s) for s in adapter.schemas(include_system=include)]}
    if operation == "tables":
        return {"tables": [asdict(t) for t in adapter.tables(_name(args, "schema"))]}
    if operation == "table":
        return asdict(adapter.table_detail(_name(args, "schema"), _name(args, "table")))
    if operation == "rows":
        request = _row_request(args)
        return asdict(adapter.rows(_name(args, "schema"), _name(args, "table"), request))
    return asdict(adapter.query(_statement(args)))


def _row_request(args: dict[str, object]) -> RowRequest:
    after = args.get("after")
    if after is not None and not isinstance(after, list):
        raise InspectorError(ErrorCode.BAD_REQUEST, "after must be a list")
    return RowRequest(
        limit=_int(args, "limit", DEFAULT_PAGE_ROWS), offset=_int(args, "offset", 0), after=after
    )
