"""PostgreSQL adapter. Identifiers are quoted by the driver (``psycopg.sql``), values are bound
or rendered as driver literals; nothing user supplied is ever spliced into SQL text.

Every session is read-only at the database level (``default_transaction_read_only`` plus a
``READ ONLY`` transaction) and every operation ends with a rollback.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Sequence
from typing import Any

import psycopg
from psycopg import sql
from psycopg.rows import TupleRow

from backend.inspector.model import (
    CELL_MAX_BYTES,
    CONSOLE_MAX_ROWS,
    ColumnInfo,
    ConnectionSpec,
    ErrorCode,
    ForeignKeyInfo,
    IndexInfo,
    InspectorError,
    Limits,
    QueryResult,
    ResultColumn,
    RowPage,
    RowRequest,
    SchemaInfo,
    ServerInfo,
    TableDetail,
    TableInfo,
    TlsMode,
)
from backend.inspector.paging import (
    ORDERING_COLUMNS,
    ORDERING_NONE,
    ORDERING_PRIMARY_KEY,
    Mode,
    PagePlan,
    SelectColumn,
    bounded_console_rows,
    build_page,
    check_request,
)
from backend.inspector.sql_guard import validate_statement

SYSTEM_SCHEMAS = frozenset({"information_schema"})
KIND_NAMES = {
    "r": "table",
    "p": "partitioned_table",
    "v": "view",
    "m": "materialized_view",
    "f": "foreign_table",
}
ACTIONS = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE", "n": "SET NULL", "d": "SET DEFAULT"}
NATIVE_CATEGORIES = frozenset({"N", "B"})
SORTABLE_CATEGORIES = frozenset({"N", "S", "D", "T", "B"})
TEXT_FETCH_CHARS = CELL_MAX_BYTES + 1
SERVER_SIDE_KINDS = frozenset({"SELECT", "WITH"})
ERROR_TEXT_LIMIT = 400

Row = tuple[Any, ...]


def _is_system_schema(name: str) -> bool:
    return name in SYSTEM_SCHEMAS or name.startswith("pg_")


class PostgresAdapter:
    """One read-only session to a PostgreSQL server."""

    def __init__(self, spec: ConnectionSpec, address: str, limits: Limits) -> None:
        self._spec = spec
        self._limits = limits
        self._conn = _connect(spec, address, limits)

    def close(self) -> None:
        """Roll back and close the connection."""
        with contextlib.suppress(psycopg.Error):
            self._conn.rollback()
        self._conn.close()

    # --- metadata -----------------------------------------------------------------

    def server_info(self) -> ServerInfo:
        """Version, session user and read-only state of the session."""
        rows = self._fetch(
            "SELECT version(), current_user, current_database(), "
            "current_setting('transaction_read_only')"
        )
        version, user, database, read_only = rows[0]
        return ServerInfo(
            version=str(version),
            read_only=read_only == "on",
            current_user=str(user),
            database=str(database),
            tls=bool(self._conn.pgconn.ssl_in_use),
        )

    def schemas(self, *, include_system: bool = False) -> list[SchemaInfo]:
        """Schemas of the database."""
        rows = self._fetch("SELECT nspname FROM pg_namespace ORDER BY nspname")
        items = [SchemaInfo(str(r[0]), _is_system_schema(str(r[0]))) for r in rows]
        return [s for s in items if include_system or not s.system]

    def tables(self, schema: str) -> list[TableInfo]:
        """Tables and views of ``schema`` with planner row estimates."""
        self._require_schema(schema)
        rows = self._fetch(
            "SELECT c.relname, c.relkind, c.reltuples::bigint FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = %s AND c.relkind IN ('r','p','v','m','f') ORDER BY c.relname",
            (schema,),
        )
        return [_table_info(schema, r) for r in rows]

    def table_detail(self, schema: str, table: str) -> TableDetail:
        """Columns, indexes and foreign keys of one table."""
        info = self._require_table(schema, table)
        columns = [column for column, _category, _typname in self._columns(schema, table)]
        return TableDetail(
            info, columns, self._indexes(schema, table), self._foreign_keys(schema, table)
        )

    def _require_schema(self, schema: str) -> None:
        if not self._fetch("SELECT 1 FROM pg_namespace WHERE nspname = %s", (schema,)):
            raise InspectorError(ErrorCode.NOT_FOUND, f"schema not found: {schema}")

    def _require_table(self, schema: str, table: str) -> TableInfo:
        for info in self.tables(schema):
            if info.name == table:
                return info
        raise InspectorError(ErrorCode.NOT_FOUND, f"table not found: {schema}.{table}")

    def _columns(self, schema: str, table: str) -> list[tuple[ColumnInfo, str, str]]:
        pk = self._primary_key_columns(schema, table)
        rows = self._fetch(
            "SELECT a.attname, a.attnum, format_type(a.atttypid, a.atttypmod), NOT a.attnotnull, "
            "pg_get_expr(d.adbin, d.adrelid), a.attidentity, a.attgenerated, "
            "t.typcategory, t.typname "
            "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace JOIN pg_type t ON t.oid = a.atttypid "
            "LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
            "WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped "
            "ORDER BY a.attnum",
            (schema, table),
        )
        result = []
        for name, num, data_type, nullable, default, identity, generated, category, typname in rows:
            extra = _column_extra(str(identity), str(generated))
            column = ColumnInfo(
                str(name),
                int(num),
                str(data_type),
                bool(nullable),
                None if default is None else str(default),
                pk.get(str(name)),
                extra,
            )
            result.append((column, str(category), str(typname)))
        return result

    def _primary_key_columns(self, schema: str, table: str) -> dict[str, int]:
        rows = self._fetch(
            "SELECT a.attname, k.ord FROM pg_constraint con "
            "JOIN pg_class c ON c.oid = con.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
            "CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) "
            "JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum "
            "WHERE con.contype = 'p' AND n.nspname = %s AND c.relname = %s",
            (schema, table),
        )
        return {str(r[0]): int(r[1]) for r in rows}

    def _indexes(self, schema: str, table: str) -> list[IndexInfo]:
        rows = self._fetch(
            "SELECT ic.relname, i.indisunique, i.indisprimary, am.amname, ARRAY("
            "SELECT COALESCE(a.attname, pg_get_indexdef(i.indexrelid, k.ord::int, true)) "
            "FROM unnest(i.indkey::int2[]) WITH ORDINALITY AS k(attnum, ord) "
            "LEFT JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum "
            "WHERE k.ord <= i.indnkeyatts ORDER BY k.ord) "
            "FROM pg_index i JOIN pg_class c ON c.oid = i.indrelid "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_class ic ON ic.oid = i.indexrelid "
            "JOIN pg_am am ON am.oid = ic.relam "
            "WHERE n.nspname = %s AND c.relname = %s ORDER BY ic.relname",
            (schema, table),
        )
        return [
            IndexInfo(str(r[0]), [str(c) for c in r[4]], bool(r[1]), bool(r[2]), str(r[3]))
            for r in rows
        ]

    def _foreign_keys(self, schema: str, table: str) -> list[ForeignKeyInfo]:
        names = (
            "SELECT a.attname FROM unnest({keys}) WITH ORDINALITY AS k(attnum, ord) "
            "JOIN pg_attribute a ON a.attrelid = {rel} AND a.attnum = k.attnum ORDER BY k.ord"
        )
        local = names.format(keys="con.conkey", rel="con.conrelid")
        remote = names.format(keys="con.confkey", rel="con.confrelid")
        rows = self._fetch(
            f"SELECT con.conname, ARRAY({local}), rn.nspname, rc.relname, ARRAY({remote}), "  # noqa: S608
            "con.confupdtype, con.confdeltype FROM pg_constraint con "
            "JOIN pg_class c ON c.oid = con.conrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_class rc ON rc.oid = con.confrelid "
            "JOIN pg_namespace rn ON rn.oid = rc.relnamespace "
            "WHERE con.contype = 'f' AND n.nspname = %s AND c.relname = %s ORDER BY con.conname",
            (schema, table),
        )
        return [
            ForeignKeyInfo(
                str(r[0]),
                [str(c) for c in r[1]],
                str(r[2]),
                str(r[3]),
                [str(c) for c in r[4]],
                ACTIONS.get(str(r[5]), str(r[5])),
                ACTIONS.get(str(r[6]), str(r[6])),
            )
            for r in rows
        ]

    # --- rows ---------------------------------------------------------------------

    def rows(self, schema: str, table: str, request: RowRequest) -> RowPage:
        """One page of ``schema.table`` ordered by primary key (or by all sortable columns)."""
        self._require_table(schema, table)
        columns = self._columns(schema, table)
        plan = _plan(columns)
        check_request(request, plan)
        query = _page_query(schema, table, plan, request)
        fetched = self._fetch_composed(query)
        return build_page(plan, fetched, request)

    # --- console ------------------------------------------------------------------

    def query(self, statement: str) -> QueryResult:
        """Validate and run one read-only statement (limited rows, always rolled back)."""
        validated = validate_statement(statement, self._spec.engine)
        return self.run_unchecked(validated.text, validated.kind)

    def run_unchecked(self, statement: str, kind: str | None = None) -> QueryResult:
        """Run ``statement`` with NO parser check; only the read-only session protects the
        database. Public so tests can prove that layer on its own."""
        started = time.monotonic()
        kind = kind or (statement.split(None, 1) or [""])[0].upper()
        try:
            columns, fetched = self._run_console(statement, kind)
        except psycopg.Error as error:
            raise _map_error(error, self._spec) from error
        finally:
            self._rollback()
        rows, size_limited = bounded_console_rows(fetched)
        truncated = len(fetched) > CONSOLE_MAX_ROWS or size_limited
        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            size_limited=size_limited,
            limit=CONSOLE_MAX_ROWS,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            statement_kind=kind,
        )

    def _run_console(self, statement: str, kind: str) -> tuple[list[ResultColumn], list[Row]]:
        if kind in SERVER_SIDE_KINDS:
            with self._conn.cursor(name="inspector_console") as cursor:
                cursor.execute(statement)
                fetched = cursor.fetchmany(CONSOLE_MAX_ROWS + 1)
                return self._describe(cursor.description), fetched
        with self._conn.cursor() as plain:
            # prepare=True uses the extended protocol, which refuses ';'-chained statements.
            plain.execute(statement, prepare=True)
            description = self._describe(plain.description)
            return description, plain.fetchmany(CONSOLE_MAX_ROWS + 1) if plain.description else []

    def _describe(self, description: Any) -> list[ResultColumn]:
        if not description:
            return []
        result = []
        for column in description:
            known = self._conn.adapters.types.get(column.type_code)
            result.append(ResultColumn(column.name, known.name if known else None))
        return result

    # --- plumbing -----------------------------------------------------------------

    def _fetch(self, query: str, params: Sequence[object] = ()) -> list[Row]:
        try:
            with self._conn.cursor() as cursor:
                cursor.execute(query, params)
                return cursor.fetchall()
        except psycopg.Error as error:
            raise _map_error(error, self._spec) from error
        finally:
            self._rollback()

    def _fetch_composed(self, query: sql.Composed) -> list[Row]:
        try:
            with self._conn.cursor() as cursor:
                cursor.execute(query)
                return cursor.fetchall()
        except psycopg.Error as error:
            raise _map_error(error, self._spec) from error
        finally:
            self._rollback()

    def _rollback(self) -> None:
        with contextlib.suppress(psycopg.Error):
            self._conn.rollback()


def _connect(spec: ConnectionSpec, address: str, limits: Limits) -> psycopg.Connection[TupleRow]:
    options = (
        "-c default_transaction_read_only=on "
        f"-c statement_timeout={limits.statement_timeout_ms} "
        "-c lock_timeout=2000 -c idle_in_transaction_session_timeout=30000 "
        "-c standard_conforming_strings=on"
    )
    try:
        conn = psycopg.connect(
            host=spec.host,
            hostaddr=address,
            port=spec.port,
            dbname=spec.database,
            user=spec.username,
            password=spec.password,
            sslmode="require" if spec.tls is TlsMode.REQUIRE else "disable",
            connect_timeout=limits.connect_timeout_s,
            options=options,
            application_name="jendock-inspector",
        )
    except psycopg.Error as error:
        message = _clean(str(error).strip().splitlines()[0] if str(error).strip() else "", spec)
        raise InspectorError(
            ErrorCode.CONNECT_FAILED, message or "cannot connect to the database"
        ) from error
    conn.read_only = True
    return conn


def _column_extra(identity: str, generated: str) -> str | None:
    if identity == "a":
        return "identity always"
    if identity == "d":
        return "identity by default"
    if generated == "s":
        return "generated stored"
    return None


def _table_info(schema: str, row: Row) -> TableInfo:
    kind = KIND_NAMES.get(str(row[1]), str(row[1]))
    estimate = int(row[2])
    approx = estimate if estimate >= 0 and kind not in {"view"} else None
    return TableInfo(schema, str(row[0]), kind, approx)


def _plan(columns: list[tuple[ColumnInfo, str, str]]) -> PagePlan:
    select = [
        SelectColumn(c.name, c.data_type, _mode(category, typname))
        for c, category, typname in columns
    ]
    keys = sorted(
        (i for i, (c, _cat, _typ) in enumerate(columns) if c.primary_key_position is not None),
        key=lambda i: columns[i][0].primary_key_position or 0,
    )
    if keys:
        return PagePlan(select, keys, [select[i].name for i in keys], ORDERING_PRIMARY_KEY)
    sortable = [
        c.name
        for c, category, typname in columns
        if category in SORTABLE_CATEGORIES and typname not in {"bytea", "json", "jsonb", "xml"}
    ]
    return PagePlan(select, [], sortable, ORDERING_COLUMNS if sortable else ORDERING_NONE)


def _mode(category: str, typname: str) -> Mode:
    if typname == "bytea":
        return Mode.BINARY
    if category in NATIVE_CATEGORIES:
        return Mode.NATIVE
    return Mode.TEXT


def _select_expression(column: SelectColumn) -> sql.Composable:
    name = sql.Identifier(column.name)
    if column.mode is Mode.NATIVE:
        return name
    if column.mode is Mode.BINARY:
        return sql.SQL("substring({c} from 1 for {n})").format(c=name, n=sql.Literal(64))
    return sql.SQL("left({c}::text, {n})").format(c=name, n=sql.Literal(TEXT_FETCH_CHARS))


def _page_query(schema: str, table: str, plan: PagePlan, request: RowRequest) -> sql.Composed:
    expressions = [_select_expression(c) for c in plan.select]
    expressions += [
        sql.SQL("octet_length({})").format(sql.Identifier(c.name))
        for c in plan.select
        if c.mode is Mode.BINARY
    ]
    parts: list[sql.Composable] = [
        sql.SQL("SELECT {cols} FROM {tbl}").format(
            cols=sql.SQL(", ").join(expressions), tbl=sql.Identifier(schema, table)
        )
    ]
    if request.after is not None:
        keys = sql.SQL(", ").join(sql.Identifier(n) for n in plan.order_names)
        values = sql.SQL(", ").join(sql.Literal(v) for v in request.after)
        parts.append(sql.SQL(" WHERE ({k}) > ({v})").format(k=keys, v=values))
    if plan.order_names:
        order = sql.SQL(", ").join(sql.Identifier(n) for n in plan.order_names)
        parts.append(sql.SQL(" ORDER BY {o}").format(o=order))
    offset = 0 if request.after is not None else request.offset
    parts.append(
        sql.SQL(" LIMIT {n} OFFSET {o}").format(
            n=sql.Literal(request.limit + 1), o=sql.Literal(offset)
        )
    )
    return sql.Composed(parts)


def _map_error(error: psycopg.Error, spec: ConnectionSpec) -> InspectorError:
    if isinstance(error, psycopg.errors.QueryCanceled):
        return InspectorError(ErrorCode.TIMEOUT, "statement exceeded the time limit")
    message = _clean(str(error).strip().splitlines()[0] if str(error).strip() else "", spec)
    if isinstance(error, psycopg.OperationalError) and not _is_server_error(error):
        return InspectorError(ErrorCode.CONNECT_FAILED, message or "cannot connect to the database")
    return InspectorError(ErrorCode.QUERY_FAILED, message or "query failed")


def _is_server_error(error: psycopg.Error) -> bool:
    return error.sqlstate is not None and not error.sqlstate.startswith("08")


def _clean(message: str, spec: ConnectionSpec) -> str:
    if spec.password:
        message = message.replace(spec.password, "***")
    return message[:ERROR_TEXT_LIMIT]
