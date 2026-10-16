"""Database inspector endpoints.

Configuration (create, change, delete, test) needs ``admin``. Metadata needs ``read``. Rows and
the console need ``read`` on development and ``admin`` on production. The API never connects to
a database or sees a password: reads and the console go through the worker, which runs a helper
container on the environment's network.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from backend.api.deps import Container, require
from backend.api.principal import Principal
from backend.api.schemas.databases import (
    ConnectionResponse,
    ConnectionTestResponse,
    CreateConnectionRequest,
    DetectResponse,
    QueryRequest,
    QueryResponse,
    RowsResponse,
    SchemaListResponse,
    TableDetailResponse,
    TableListResponse,
    UpdateConnectionRequest,
    decode_cursor,
    parse_worker,
    rows_response,
)
from backend.domain.access import Action
from backend.domain.database import ConnectionRef, TableRef
from backend.domain.enums import EnvironmentName
from backend.inspector.model import DEFAULT_PAGE_ROWS, MAX_PAGE_ROWS
from backend.services.database_connections import ConnectionChange, NewConnection

router = APIRouter(prefix="/api/projects/{slug}/environments/{env}/databases", tags=["databases"])
Reader = Annotated[Principal, Depends(require(Action.DATABASE_READ))]
Configurer = Annotated[Principal, Depends(require(Action.DATABASE_CONFIGURE))]
DataReader = Annotated[Principal, Depends(require(Action.DATABASE_DATA))]
Detector = Annotated[Principal, Depends(require(Action.DATABASE_DETECT))]
MAX_OFFSET = 10_000_000


@dataclass(frozen=True, slots=True)
class EnvPath:
    """Path parameters identifying an environment."""

    slug: str
    env: EnvironmentName


@dataclass(frozen=True, slots=True)
class ConnectionPath:
    """Path parameters identifying one connection."""

    slug: str
    env: EnvironmentName
    name: str

    @property
    def ref(self) -> ConnectionRef:
        """The connection as a reference for the services."""
        return ConnectionRef(self.slug, self.env, self.name)


@dataclass(frozen=True, slots=True)
class SchemaPath(ConnectionPath):
    """Path parameters identifying a schema of a connection."""

    schema: str


@dataclass(frozen=True, slots=True)
class TablePath(SchemaPath):
    """Path parameters identifying a table of a connection."""

    table: str

    @property
    def table_ref(self) -> TableRef:
        """The table as a reference for the services."""
        return TableRef(self.ref, self.schema, self.table)


# --- configuration -------------------------------------------------------------------------


@router.get("")
def list_connections(
    path: Annotated[EnvPath, Depends()], _principal: Reader, container: Container
) -> list[ConnectionResponse]:
    """List the environment's database connections."""
    found = container.database_connections().list(path.slug, path.env)
    return [ConnectionResponse.from_model(c, path.env) for c in found]


@router.post("", status_code=201)
def create_connection(
    path: Annotated[EnvPath, Depends()],
    body: CreateConnectionRequest,
    principal: Configurer,
    container: Container,
) -> ConnectionResponse:
    """Create a connection; the password is stored in the environment's secret store."""
    new = NewConnection(
        name=body.name,
        engine=body.engine,
        host=body.host,
        database=body.database,
        username=body.username,
        password=body.password,
        port=body.port,
        tls_mode=body.tls_mode,
    )
    created = container.database_connections().create(path.slug, path.env, new, principal.actor)
    return ConnectionResponse.from_model(created, path.env)


@router.post("/detect")
def detect_connections(
    path: Annotated[EnvPath, Depends()], principal: Detector, container: Container
) -> DetectResponse:
    """Detect, store and test environment database connections inside the worker."""
    result = container.inspector_gateway().detect(path.slug, path.env, principal.actor)
    return parse_worker(DetectResponse, result)


@router.get("/{name}")
def get_connection(
    path: Annotated[ConnectionPath, Depends()], _principal: Reader, container: Container
) -> ConnectionResponse:
    """One connection (never its password)."""
    found = container.database_connections().get(path.slug, path.env, path.name)
    return ConnectionResponse.from_model(found, path.env)


@router.patch("/{name}")
def update_connection(
    path: Annotated[ConnectionPath, Depends()],
    body: UpdateConnectionRequest,
    principal: Configurer,
    container: Container,
) -> ConnectionResponse:
    """Change a connection; a ``password`` rotates the stored secret."""
    change = ConnectionChange(**body.model_dump(exclude_none=True))
    updated = container.database_connections().update(
        path.slug, path.env, path.name, change, principal.actor
    )
    return ConnectionResponse.from_model(updated, path.env)


@router.delete("/{name}", status_code=204)
def delete_connection(
    path: Annotated[ConnectionPath, Depends()], principal: Configurer, container: Container
) -> Response:
    """Delete a connection and its password secret."""
    container.database_connections().delete(path.slug, path.env, path.name, principal.actor)
    return Response(status_code=204)


@router.post("/{name}/test")
def test_connection(
    path: Annotated[ConnectionPath, Depends()], principal: Configurer, container: Container
) -> ConnectionTestResponse:
    """Connect through the environment network; the answer is 200 with ``ok`` true or false."""
    result = container.inspector_gateway().test(path.ref, principal.actor)
    return parse_worker(ConnectionTestResponse, result)


# --- metadata ------------------------------------------------------------------------------


@router.get("/{name}/schemas")
def list_schemas(
    path: Annotated[ConnectionPath, Depends()],
    principal: Reader,
    container: Container,
    include_system: bool = False,
) -> SchemaListResponse:
    """Schemas (system schemas only with ``include_system=true``)."""
    result = container.inspector_gateway().schemas(
        path.ref, principal.actor, include_system=include_system
    )
    return parse_worker(SchemaListResponse, result)


@router.get("/{name}/schemas/{schema}/tables")
def list_tables(
    path: Annotated[SchemaPath, Depends()], principal: Reader, container: Container
) -> TableListResponse:
    """Tables and views of a schema with approximate row counts."""
    result = container.inspector_gateway().tables(path.ref, path.schema, principal.actor)
    return parse_worker(TableListResponse, result)


@router.get("/{name}/schemas/{schema}/tables/{table}")
def get_table(
    path: Annotated[TablePath, Depends()], principal: Reader, container: Container
) -> TableDetailResponse:
    """Columns, indexes and foreign keys of a table."""
    result = container.inspector_gateway().table(path.table_ref, principal.actor)
    return parse_worker(TableDetailResponse, result)


# --- data ----------------------------------------------------------------------------------


@router.get("/{name}/schemas/{schema}/tables/{table}/rows")
def get_rows(  # noqa: PLR0913, PLR0917 - path, scope, container and three paging parameters
    path: Annotated[TablePath, Depends()],
    principal: DataReader,
    container: Container,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_ROWS)] = DEFAULT_PAGE_ROWS,
    offset: Annotated[int, Query(ge=0, le=MAX_OFFSET)] = 0,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
) -> RowsResponse:
    """One page of rows ordered by primary key (``cursor`` keyset paging beats ``offset``)."""
    page: dict[str, object] = {"limit": limit, "offset": offset}
    if cursor is not None:
        page["after"] = decode_cursor(cursor)
    result = container.inspector_gateway().rows(path.table_ref, page, principal.actor)
    return rows_response(result)


@router.post("/{name}/query")
def run_query(
    path: Annotated[ConnectionPath, Depends()],
    body: QueryRequest,
    principal: DataReader,
    container: Container,
) -> QueryResponse:
    """Run one read-only statement (SELECT, WITH, SHOW or EXPLAIN); audited, results are not."""
    result = container.inspector_gateway().query(path.ref, body.statement, principal.actor)
    return parse_worker(QueryResponse, result)
