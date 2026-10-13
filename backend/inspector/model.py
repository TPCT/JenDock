"""Plain data types of the inspector and its limits. JSON shapes are produced by ``asdict``."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

MAX_PAGE_ROWS = 500
DEFAULT_PAGE_ROWS = 100
CONSOLE_MAX_ROWS = 500
CONSOLE_TIMEOUT_MS = 5_000
METADATA_TIMEOUT_MS = 10_000
CELL_MAX_BYTES = 4096
BINARY_PREVIEW_BYTES = 64
MAX_STATEMENT_CHARS = 20_000
# Serialized size ceiling of one result (the worker RPC allows 1 MiB per message).
RESULT_MAX_BYTES = 640 * 1024
CONNECT_TIMEOUT_S = 5


class Engine(StrEnum):
    """Supported database engines."""

    POSTGRESQL = "postgresql"
    MYSQL = "mysql"
    MARIADB = "mariadb"


class TlsMode(StrEnum):
    """Transport security: ``require`` encrypts without verifying the certificate."""

    DISABLE = "disable"
    REQUIRE = "require"


class ErrorCode(StrEnum):
    """Machine-readable failure categories of the inspector."""

    REJECTED = "rejected"
    TIMEOUT = "timeout"
    CONNECT_FAILED = "connect_failed"
    NETWORK_DENIED = "network_denied"
    QUERY_FAILED = "query_failed"
    NOT_FOUND = "not_found"
    BAD_REQUEST = "bad_request"
    HELPER_FAILED = "helper_failed"


class InspectorError(Exception):
    """A failure with a stable code and a message that is safe to show to the user."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class ConnectionSpec:
    """Where and how to connect. ``password`` never appears in ``repr``."""

    engine: Engine
    host: str
    port: int
    database: str
    username: str
    password: str = field(repr=False)
    tls: TlsMode = TlsMode.DISABLE


@dataclass(frozen=True, slots=True)
class Limits:
    """Per-operation limits."""

    statement_timeout_ms: int = METADATA_TIMEOUT_MS
    connect_timeout_s: int = CONNECT_TIMEOUT_S


@dataclass(frozen=True, slots=True)
class SchemaInfo:
    """A schema (PostgreSQL schema or MySQL database)."""

    name: str
    system: bool


@dataclass(frozen=True, slots=True)
class TableInfo:
    """A table-like relation; ``approx_rows`` is the planner/engine estimate or ``None``."""

    schema: str
    name: str
    kind: str
    approx_rows: int | None


@dataclass(frozen=True, slots=True)
class ColumnInfo:
    """One column; ``primary_key_position`` is 1-based within the primary key."""

    name: str
    position: int
    data_type: str
    nullable: bool
    default: str | None
    primary_key_position: int | None
    extra: str | None


@dataclass(frozen=True, slots=True)
class IndexInfo:
    """One index with its key columns (expressions are shown as text)."""

    name: str
    columns: list[str]
    unique: bool
    primary: bool
    method: str | None


@dataclass(frozen=True, slots=True)
class ForeignKeyInfo:
    """One foreign key constraint."""

    name: str
    columns: list[str]
    ref_schema: str
    ref_table: str
    ref_columns: list[str]
    on_update: str
    on_delete: str


@dataclass(frozen=True, slots=True)
class TableDetail:
    """Everything the table page needs in one call."""

    table: TableInfo
    columns: list[ColumnInfo]
    indexes: list[IndexInfo]
    foreign_keys: list[ForeignKeyInfo]


@dataclass(frozen=True, slots=True)
class RowRequest:
    """A page request: keyset (``after``) wins over ``offset`` when both are given."""

    limit: int = DEFAULT_PAGE_ROWS
    offset: int = 0
    after: list[object] | None = None


@dataclass(frozen=True, slots=True)
class ResultColumn:
    """A column of a result set."""

    name: str
    type: str | None = None


@dataclass(frozen=True, slots=True)
class RowPage:
    """One page of table rows. Cells are JSON values or small marker objects."""

    columns: list[ResultColumn]
    rows: list[list[object]]
    limit: int
    offset: int
    ordering: str
    order_by: list[str]
    next_after: list[object] | None
    next_offset: int | None
    size_limited: bool


@dataclass(frozen=True, slots=True)
class QueryResult:
    """Result of a console statement."""

    columns: list[ResultColumn]
    rows: list[list[object]]
    row_count: int
    truncated: bool
    size_limited: bool
    limit: int
    elapsed_ms: int
    statement_kind: str


@dataclass(frozen=True, slots=True)
class ServerInfo:
    """What ``test`` reports about a reachable server."""

    version: str
    read_only: bool
    current_user: str
    database: str
    tls: bool
