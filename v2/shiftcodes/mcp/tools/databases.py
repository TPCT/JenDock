"""Database inspector: connections (no secrets), metadata, paginated rows and a read-only console.

Mirrors ``api/routers/databases.py``: connection listing and metadata need ``read``; rows and the
console need ``read`` on development and ``admin`` on production (the shared
``require_data_scope`` rule, enforced inside the handler because it depends on the environment).
The worker runs every query in a helper container; passwords never reach the API or MCP.

Deliberate gap: creating, changing, deleting or testing a connection is operator-only (``admin``
REST endpoints) and is not exposed over MCP, so an agent can never read or change credentials.
"""

from __future__ import annotations

from shiftcodes.api.data_scope import require_data_scope
from shiftcodes.api.schemas.databases import (
    ConnectionResponse,
    QueryRequest,
    QueryResponse,
    SchemaListResponse,
    TableDetailResponse,
    TableListResponse,
    decode_cursor,
    parse_worker,
    rows_response,
)
from shiftcodes.domain.database import ConnectionRef, TableRef
from shiftcodes.domain.enums import TokenScope
from shiftcodes.inspector.model import DEFAULT_PAGE_ROWS, MAX_PAGE_ROWS, MAX_STATEMENT_CHARS
from shiftcodes.mcp import schema
from shiftcodes.mcp.registry import Args, JsonSchema, Tool, ToolContext

_NAME: JsonSchema = {"type": "string", "minLength": 1, "maxLength": 128}
_IDENT: JsonSchema = {"type": "string", "minLength": 1, "maxLength": 200}
_ENV = {"slug": schema.SLUG, "environment": schema.ENVIRONMENT}
_CONNECTION = {**_ENV, "connection": _NAME}
_REQUIRED_ENV = ["slug", "environment"]
_REQUIRED_CONNECTION = [*_REQUIRED_ENV, "connection"]
_SCHEMA = {**_CONNECTION, "schema": _IDENT}
_TABLE = {**_SCHEMA, "table": _IDENT}


def _ref(args: Args) -> ConnectionRef:
    return ConnectionRef(args.text("slug"), args.environment(), args.text("connection"))


def _table(args: Args) -> TableRef:
    return TableRef(_ref(args), args.text("schema"), args.text("table"))


def list_connections(ctx: ToolContext, args: Args) -> object:
    """The environment's database connections (never a password)."""
    env = args.environment()
    found = ctx.container.database_connections().list(args.text("slug"), env)
    return [ConnectionResponse.from_model(c, env).model_dump(mode="json") for c in found]


def get_connection(ctx: ToolContext, args: Args) -> object:
    """One connection."""
    env = args.environment()
    found = ctx.container.database_connections().get(
        args.text("slug"), env, args.text("connection")
    )
    return ConnectionResponse.from_model(found, env).model_dump(mode="json")


def list_schemas(ctx: ToolContext, args: Args) -> object:
    """Schemas of a connection (system schemas only when asked)."""
    include = bool(args.values.get("include_system", False))
    result = ctx.container.inspector_gateway().schemas(
        _ref(args), ctx.actor, include_system=include
    )
    return parse_worker(SchemaListResponse, result).model_dump(mode="json")


def list_tables(ctx: ToolContext, args: Args) -> object:
    """Tables and views of a schema with approximate row counts."""
    result = ctx.container.inspector_gateway().tables(_ref(args), args.text("schema"), ctx.actor)
    return parse_worker(TableListResponse, result).model_dump(mode="json")


def get_table(ctx: ToolContext, args: Args) -> object:
    """Columns, indexes and foreign keys of a table."""
    result = ctx.container.inspector_gateway().table(_table(args), ctx.actor)
    return parse_worker(TableDetailResponse, result).model_dump(mode="json")


def read_rows(ctx: ToolContext, args: Args) -> object:
    """One page of rows ordered by primary key; continue with the opaque next_cursor."""
    require_data_scope(ctx.principal, args.environment())
    page: dict[str, object] = {
        "limit": args.integer("limit", DEFAULT_PAGE_ROWS),
        "offset": args.integer("offset", 0),
    }
    cursor = args.optional_text("cursor")
    if cursor is not None:
        page["after"] = decode_cursor(cursor)
    result = ctx.container.inspector_gateway().rows(_table(args), page, ctx.actor)
    return rows_response(result).model_dump(mode="json")


def run_query(ctx: ToolContext, args: Args) -> object:
    """Run one read-only statement (SELECT, WITH, SHOW or EXPLAIN); the statement is audited,
    the results are not."""
    require_data_scope(ctx.principal, args.environment())
    statement = QueryRequest(statement=args.text("statement")).statement
    result = ctx.container.inspector_gateway().query(_ref(args), statement, ctx.actor)
    return parse_worker(QueryResponse, result).model_dump(mode="json")


TOOLS = (
    Tool(
        "list_database_connections",
        "List the database connections configured for an environment (engine, host, database, "
        "user, TLS mode, last test). Passwords are never shown.",
        TokenScope.READ,
        schema.obj(_ENV, _REQUIRED_ENV),
        list_connections,
    ),
    Tool(
        "get_database_connection",
        "Get one database connection by name.",
        TokenScope.READ,
        schema.obj(_CONNECTION, _REQUIRED_CONNECTION),
        get_connection,
    ),
    Tool(
        "list_database_schemas",
        "List the schemas of a database connection.",
        TokenScope.READ,
        schema.obj(
            {**_CONNECTION, "include_system": {"type": "boolean", "default": False}},
            _REQUIRED_CONNECTION,
        ),
        list_schemas,
    ),
    Tool(
        "list_database_tables",
        "List the tables and views of a schema with approximate row counts.",
        TokenScope.READ,
        schema.obj(_SCHEMA, [*_REQUIRED_CONNECTION, "schema"]),
        list_tables,
    ),
    Tool(
        "get_database_table",
        "Columns, indexes and foreign keys of a table.",
        TokenScope.READ,
        schema.obj(_TABLE, [*_REQUIRED_CONNECTION, "schema", "table"]),
        get_table,
    ),
    Tool(
        "read_table_rows",
        "Read a page of rows of a table, ordered by primary key (at most "
        f"{MAX_PAGE_ROWS}). Pass the returned next_cursor to continue (keyset paging beats "
        "offset). Needs the read scope on development and the admin scope on production.",
        TokenScope.READ,
        schema.obj(
            {
                **_TABLE,
                "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PAGE_ROWS},
                "offset": {"type": "integer", "minimum": 0, "maximum": 10_000_000},
                "cursor": {"type": "string", "maxLength": 2048},
            },
            [*_REQUIRED_CONNECTION, "schema", "table"],
        ),
        read_rows,
    ),
    Tool(
        "run_readonly_query",
        "Run one read-only statement (SELECT, WITH, SHOW or EXPLAIN) on a database connection. "
        "The statement text is audited, results are not stored. Needs the read scope on "
        "development and the admin scope on production.",
        TokenScope.READ,
        schema.obj(
            {
                **_CONNECTION,
                "statement": {"type": "string", "minLength": 1, "maxLength": MAX_STATEMENT_CHARS},
            },
            [*_REQUIRED_CONNECTION, "statement"],
        ),
        run_query,
    ),
)
