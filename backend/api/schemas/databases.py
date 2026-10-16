"""Database inspector schemas. Cells are plain JSON scalars or small marker objects
(``{"truncated": true, ...}`` / ``{"binary": true, ...}``); see ``docs/api-contract.md``."""

from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from backend.domain.database import DatabaseEngine, TlsMode, password_secret_name
from backend.domain.enums import EnvironmentName
from backend.domain.errors import ValidationError, WorkerError
from backend.inspector.model import MAX_STATEMENT_CHARS
from backend.repositories.models import DatabaseConnection

MAX_PASSWORD_LENGTH = 1024
MAX_CURSOR_LENGTH = 2048
MAX_CURSOR_VALUES = 8
PRODUCTION_NOTICE = (
    "Production connection: sessions are opened read-only, but the strongest guarantee is the "
    "database user itself. Use a dedicated user that only has SELECT privileges."
)


class CreateConnectionRequest(BaseModel):
    """Body of ``POST .../databases``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=48)
    engine: DatabaseEngine
    host: str = Field(min_length=1, max_length=253)
    port: int | None = Field(default=None, ge=1, le=65535)
    database: str = Field(min_length=1, max_length=128)
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=MAX_PASSWORD_LENGTH, repr=False)
    tls_mode: TlsMode = TlsMode.DISABLE


class UpdateConnectionRequest(BaseModel):
    """Body of ``PATCH .../databases/{name}``; omitted fields stay unchanged."""

    model_config = ConfigDict(extra="forbid")

    engine: DatabaseEngine | None = None
    host: str | None = Field(default=None, min_length=1, max_length=253)
    port: int | None = Field(default=None, ge=1, le=65535)
    database: str | None = Field(default=None, min_length=1, max_length=128)
    username: str | None = Field(default=None, min_length=1, max_length=128)
    password: str | None = Field(
        default=None, min_length=1, max_length=MAX_PASSWORD_LENGTH, repr=False
    )
    tls_mode: TlsMode | None = None


class QueryRequest(BaseModel):
    """Body of ``POST .../databases/{name}/query``."""

    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1, max_length=MAX_STATEMENT_CHARS * 2)


class ConnectionResponse(BaseModel):
    """A connection. The password is never returned; ``password_secret`` names where it is."""

    name: str
    engine: DatabaseEngine
    host: str
    port: int
    database: str
    username: str
    tls_mode: TlsMode
    network: str | None = None
    auto_detected: bool = False
    password_secret: str
    read_only: bool = True
    environment: EnvironmentName
    notice: str | None
    created_at: datetime
    updated_at: datetime
    last_tested_at: datetime | None
    last_test_ok: bool | None

    @classmethod
    def from_model(cls, connection: DatabaseConnection, env: EnvironmentName) -> ConnectionResponse:
        """Build the response from a stored connection."""
        return cls(
            name=connection.name,
            engine=connection.engine,
            host=connection.host,
            port=connection.port,
            database=connection.database,
            username=connection.username,
            tls_mode=connection.tls_mode,
            network=connection.network,
            auto_detected=connection.auto_detected,
            password_secret=password_secret_name(connection.name),
            environment=env,
            notice=PRODUCTION_NOTICE if env is EnvironmentName.PRODUCTION else None,
            created_at=connection.created_at,
            updated_at=connection.updated_at,
            last_tested_at=connection.last_tested_at,
            last_test_ok=connection.last_test_ok,
        )


class _Worker(BaseModel):
    """Base of shapes validated from worker output."""

    model_config = ConfigDict(extra="ignore")


class ServerInfoResponse(_Worker):
    """What a successful connection test learned about the server."""

    version: str
    read_only: bool
    current_user: str
    database: str
    tls: bool


class ConnectionTestError(_Worker):
    """Why a connection test failed."""

    code: str
    message: str


class ConnectionTestResponse(_Worker):
    """Result of ``POST .../test`` (always 200; ``ok`` tells)."""

    ok: bool
    error: ConnectionTestError | None
    server: ServerInfoResponse | None
    network: str | None


class DetectionItemResponse(BaseModel):
    """Safe metadata about one database container considered by detection."""

    service: str
    container: str
    image: str
    engine: DatabaseEngine | None
    status: str
    reason: str | None
    connection: ConnectionResponse | None
    test: ConnectionTestResponse | None


class DetectionKeptResponse(BaseModel):
    """A connection retained during pruning, with the reason it was protected."""

    name: str
    reason: str


class DetectResponse(BaseModel):
    """Summary and safe per-container outcomes from database detection."""

    found: int
    results: list[DetectionItemResponse]
    removed: list[str] = Field(default_factory=list)
    kept: list[DetectionKeptResponse] = Field(default_factory=list)


class SchemaResponse(_Worker):
    """One schema."""

    name: str
    system: bool


class SchemaListResponse(_Worker):
    """Schemas of a database."""

    schemas: list[SchemaResponse]


class TableResponse(_Worker):
    """A table or view; ``approx_rows`` is an estimate."""

    schema_name: str = Field(alias="schema")
    name: str
    kind: str
    approx_rows: int | None

    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class TableListResponse(_Worker):
    """Tables of a schema."""

    tables: list[TableResponse]


class ColumnResponse(_Worker):
    """One column."""

    name: str
    position: int
    data_type: str
    nullable: bool
    default: str | None
    primary_key_position: int | None
    extra: str | None


class IndexResponse(_Worker):
    """One index."""

    name: str
    columns: list[str]
    unique: bool
    primary: bool
    method: str | None


class ForeignKeyResponse(_Worker):
    """One foreign key."""

    name: str
    columns: list[str]
    ref_schema: str
    ref_table: str
    ref_columns: list[str]
    on_update: str
    on_delete: str


class TableDetailResponse(_Worker):
    """Columns, indexes and foreign keys of a table."""

    table: TableResponse
    columns: list[ColumnResponse]
    indexes: list[IndexResponse]
    foreign_keys: list[ForeignKeyResponse]


class ResultColumnResponse(_Worker):
    """A column of a result set."""

    name: str
    type: str | None


class RowsResponse(BaseModel):
    """One page of table rows; follow ``next_cursor`` (keyset) or ``next_offset``."""

    columns: list[ResultColumnResponse]
    rows: list[list[object]]
    limit: int
    offset: int
    ordering: str
    order_by: list[str]
    next_cursor: str | None
    next_offset: int | None
    size_limited: bool


class QueryResponse(_Worker):
    """Result of a console statement."""

    columns: list[ResultColumnResponse]
    rows: list[list[object]]
    row_count: int
    truncated: bool
    size_limited: bool
    limit: int
    elapsed_ms: int
    statement_kind: str


def parse_worker[M: BaseModel](model: type[M], data: object) -> M:
    """Validate worker output; a shape mismatch is a worker fault, never a client error."""
    try:
        return model.model_validate(data)
    except PydanticValidationError as error:
        raise WorkerError("worker returned an unexpected response") from error


def rows_response(data: dict[str, object]) -> RowsResponse:
    """Turn the helper's page into the API shape (keyset values become an opaque cursor)."""
    after = data.get("next_after")
    cursor = encode_cursor(after) if isinstance(after, list) else None
    payload = {**data, "next_cursor": cursor}
    payload.pop("next_after", None)
    return parse_worker(RowsResponse, payload)


def encode_cursor(values: list[object]) -> str:
    """Opaque url-safe keyset cursor."""
    raw = json.dumps(values, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> list[str | int]:
    """Parse a cursor from a client; anything else is a validation error."""
    if len(cursor) > MAX_CURSOR_LENGTH:
        raise ValidationError("invalid cursor")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        values = json.loads(raw)
    except (binascii.Error, ValueError):
        raise ValidationError("invalid cursor") from None
    valid = isinstance(values, list) and 0 < len(values) <= MAX_CURSOR_VALUES
    if not valid or not all(isinstance(v, int | str) and not isinstance(v, bool) for v in values):
        raise ValidationError("invalid cursor")
    return list(values)
