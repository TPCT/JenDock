"""MySQL and MariaDB adapter (PyMySQL).

PyMySQL has no identifier quoting API, so identifiers are quoted by ``quote_identifier`` below
and, in addition, only ever taken from the server's own catalog (a table or column name the
caller supplies is first looked up in ``information_schema`` and the catalog spelling is used).
Values are always passed as parameters.

Every session is read-only at the database level (``SET SESSION TRANSACTION READ ONLY`` plus
``START TRANSACTION READ ONLY``) and every operation ends with a rollback.
"""

from __future__ import annotations

import contextlib
import ssl
import time
from collections.abc import Sequence
from typing import Any

import pymysql
import pymysql.cursors
from pymysql.constants import FIELD_TYPE

from backend.inspector.model import (
    CELL_MAX_BYTES,
    CONSOLE_MAX_ROWS,
    ColumnInfo,
    ConnectionSpec,
    Engine,
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

SYSTEM_SCHEMAS = frozenset({"mysql", "information_schema", "performance_schema", "sys"})
SQL_MODE = (
    "ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,"
    "ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION"
)
TEXT_FETCH_CHARS = CELL_MAX_BYTES + 1
NATIVE_TYPES = frozenset(
    {"tinyint", "smallint", "mediumint", "int", "integer", "bigint", "decimal", "numeric"}
    | {"float", "double", "real", "year"}
)
BINARY_TYPES = frozenset({"binary", "varbinary", "tinyblob", "blob", "mediumblob", "longblob"})
GEOMETRY_TYPES = frozenset(
    {"geometry", "point", "linestring", "polygon", "multipoint", "multilinestring"}
    | {"multipolygon", "geometrycollection"}
)
SORTABLE_TYPES = NATIVE_TYPES | {"char", "varchar", "date", "datetime", "timestamp", "time"}
TIMEOUT_ERRORS = frozenset({3024, 1969})
CONNECTION_LOST = frozenset({2006, 2013})
ERROR_TEXT_LIMIT = 400
KIND_NAMES = {"BASE TABLE": "table", "VIEW": "view", "SYSTEM VIEW": "view", "SEQUENCE": "sequence"}
FIELD_TYPE_NAMES = {
    value: name.lower() for name, value in vars(FIELD_TYPE).items() if isinstance(value, int)
}

Row = tuple[Any, ...]


def quote_identifier(name: str) -> str:
    """Quote ``name`` as a MySQL identifier for a query that is later ``%``-formatted."""
    if "\0" in name:
        raise InspectorError(ErrorCode.BAD_REQUEST, "identifier contains a NUL character")
    return "`" + name.replace("`", "``").replace("%", "%%") + "`"


class MySqlAdapter:
    """One read-only session to a MySQL or MariaDB server."""

    def __init__(self, spec: ConnectionSpec, address: str, limits: Limits) -> None:
        self._spec = spec
        self._limits = limits
        self._mariadb = spec.engine is Engine.MARIADB
        self._conn = _connect(spec, address, limits)
        with contextlib.suppress(pymysql.Error, AttributeError):
            server_info = self._conn.get_server_info()
            if isinstance(server_info, str) and "mariadb" in server_info.lower():
                self._mariadb = True
        self._dead = False
        self._prepare_session()

    def close(self) -> None:
        """Roll back and close the connection."""
        if self._dead:
            return
        with contextlib.suppress(pymysql.Error):
            self._conn.rollback()
        with contextlib.suppress(pymysql.Error):
            self._conn.close()

    def _prepare_session(self) -> None:
        timeout = self._limits.statement_timeout_ms
        timeout_sql = (
            f"SET SESSION max_statement_time = {timeout / 1000:.3f}"
            if self._mariadb
            else f"SET SESSION max_execution_time = {int(timeout)}"
        )
        try:
            with self._conn.cursor() as cursor:
                cursor.execute(f"SET SESSION sql_mode = '{SQL_MODE}'")
                cursor.execute("SET SESSION TRANSACTION READ ONLY")
                cursor.execute(timeout_sql)
        except pymysql.Error as error:
            raise _map_error(error, self._spec, started=time.monotonic()) from error

    # --- metadata -----------------------------------------------------------------

    def server_info(self) -> ServerInfo:
        """Version, session user and read-only state of the session."""
        flag = "tx_read_only" if self._mariadb else "transaction_read_only"
        rows = self._fetch(f"SELECT VERSION(), CURRENT_USER(), DATABASE(), @@{flag}")
        version, user, database, read_only = rows[0]
        status = self._fetch("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
        return ServerInfo(
            version=str(version),
            read_only=bool(int(read_only)),
            current_user=str(user),
            database="" if database is None else str(database),
            tls=bool(status and status[0][1]),
        )

    def schemas(self, *, include_system: bool = False) -> list[SchemaInfo]:
        """Databases of the server (MySQL calls schemas databases)."""
        rows = self._fetch(
            "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA ORDER BY SCHEMA_NAME"
        )
        items = [SchemaInfo(str(r[0]), str(r[0]).lower() in SYSTEM_SCHEMAS) for r in rows]
        return [s for s in items if include_system or not s.system]

    def tables(self, schema: str) -> list[TableInfo]:
        """Tables and views of ``schema`` with the engine's row estimates."""
        self._require_schema(schema)
        rows = self._fetch(
            "SELECT TABLE_NAME, TABLE_TYPE, TABLE_ROWS FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME",
            (schema,),
        )
        return [
            TableInfo(
                schema,
                str(r[0]),
                KIND_NAMES.get(str(r[1]), str(r[1]).lower()),
                None if r[2] is None else int(r[2]),
            )
            for r in rows
        ]

    def table_detail(self, schema: str, table: str) -> TableDetail:
        """Columns, indexes and foreign keys of one table."""
        info = self._require_table(schema, table)
        columns = [column for column, _data_type in self._columns(schema, table)]
        return TableDetail(
            info, columns, self._indexes(schema, table), self._foreign_keys(schema, table)
        )

    def _require_schema(self, schema: str) -> None:
        found = self._fetch(
            "SELECT 1 FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s", (schema,)
        )
        if not found:
            raise InspectorError(ErrorCode.NOT_FOUND, f"schema not found: {schema}")

    def _require_table(self, schema: str, table: str) -> TableInfo:
        for info in self.tables(schema):
            if info.name == table:
                return info
        raise InspectorError(ErrorCode.NOT_FOUND, f"table not found: {schema}.{table}")

    def _columns(self, schema: str, table: str) -> list[tuple[ColumnInfo, str]]:
        pk = self._primary_key(schema, table)
        rows = self._fetch(
            "SELECT COLUMN_NAME, ORDINAL_POSITION, COLUMN_TYPE, IS_NULLABLE, COLUMN_DEFAULT, "
            "EXTRA, DATA_TYPE FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
            (schema, table),
        )
        result = []
        for name, position, column_type, nullable, default, extra, data_type in rows:
            nullable_flag = str(nullable).upper() == "YES"
            column = ColumnInfo(
                str(name),
                int(position),
                str(column_type),
                nullable_flag,
                self._default(default, nullable=nullable_flag),
                pk.get(str(name)),
                str(extra) or None,
            )
            result.append((column, str(data_type).lower()))
        return result

    def _default(self, value: object, *, nullable: bool) -> str | None:
        if value is None:
            return None
        text = str(value)
        if not self._mariadb:
            return text
        if text == "NULL" and nullable:
            return None
        if len(text) >= 2 and text.startswith("'") and text.endswith("'"):
            return text[1:-1].replace("''", "'")
        return text

    def _primary_key(self, schema: str, table: str) -> dict[str, int]:
        rows = self._fetch(
            "SELECT COLUMN_NAME, SEQ_IN_INDEX FROM information_schema.STATISTICS "
            "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND INDEX_NAME = 'PRIMARY'",
            (schema, table),
        )
        return {str(r[0]): int(r[1]) for r in rows}

    def _indexes(self, schema: str, table: str) -> list[IndexInfo]:
        rows = self._fetch(
            "SELECT INDEX_NAME, SEQ_IN_INDEX, COLUMN_NAME, NON_UNIQUE, INDEX_TYPE "
            "FROM information_schema.STATISTICS WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s "
            "ORDER BY INDEX_NAME, SEQ_IN_INDEX",
            (schema, table),
        )
        grouped: dict[str, IndexInfo] = {}
        for name, _seq, column, non_unique, method in rows:
            existing = grouped.get(str(name))
            label = "(expression)" if column is None else str(column)
            if existing is None:
                grouped[str(name)] = IndexInfo(
                    str(name), [label], not int(non_unique), str(name) == "PRIMARY", str(method)
                )
            else:
                existing.columns.append(label)
        return list(grouped.values())

    def _foreign_keys(self, schema: str, table: str) -> list[ForeignKeyInfo]:
        rows = self._fetch(
            "SELECT k.CONSTRAINT_NAME, k.COLUMN_NAME, k.REFERENCED_TABLE_SCHEMA, "
            "k.REFERENCED_TABLE_NAME, k.REFERENCED_COLUMN_NAME, r.UPDATE_RULE, r.DELETE_RULE "
            "FROM information_schema.KEY_COLUMN_USAGE k "
            "JOIN information_schema.REFERENTIAL_CONSTRAINTS r "
            "ON r.CONSTRAINT_SCHEMA = k.CONSTRAINT_SCHEMA "
            "AND r.CONSTRAINT_NAME = k.CONSTRAINT_NAME AND r.TABLE_NAME = k.TABLE_NAME "
            "WHERE k.TABLE_SCHEMA = %s AND k.TABLE_NAME = %s "
            "AND k.REFERENCED_TABLE_NAME IS NOT NULL "
            "ORDER BY k.CONSTRAINT_NAME, k.ORDINAL_POSITION",
            (schema, table),
        )
        grouped: dict[str, ForeignKeyInfo] = {}
        for name, column, ref_schema, ref_table, ref_column, on_update, on_delete in rows:
            existing = grouped.get(str(name))
            if existing is None:
                grouped[str(name)] = ForeignKeyInfo(
                    str(name),
                    [str(column)],
                    str(ref_schema),
                    str(ref_table),
                    [str(ref_column)],
                    str(on_update),
                    str(on_delete),
                )
            else:
                existing.columns.append(str(column))
                existing.ref_columns.append(str(ref_column))
        return list(grouped.values())

    # --- rows ---------------------------------------------------------------------

    def rows(self, schema: str, table: str, request: RowRequest) -> RowPage:
        """One page of ``schema.table`` ordered by primary key (or by all sortable columns)."""
        self._require_table(schema, table)
        columns = self._columns(schema, table)
        plan = _plan(columns)
        check_request(request, plan)
        query, params = _page_query(schema, table, plan, request)
        return build_page(plan, self._fetch(query, params), request)

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
            columns, fetched = self._run_console(statement)
        except pymysql.Error as error:
            raise _map_error(error, self._spec, started=started) from error
        except TimeoutError as error:
            raise InspectorError(ErrorCode.TIMEOUT, "statement exceeded the time limit") from error
        finally:
            self._rollback()
        rows, size_limited = bounded_console_rows(fetched)
        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=len(fetched) > CONSOLE_MAX_ROWS or size_limited,
            size_limited=size_limited,
            limit=CONSOLE_MAX_ROWS,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            statement_kind=kind,
        )

    def _run_console(self, statement: str) -> tuple[list[ResultColumn], list[Row]]:
        cursor = self._conn.cursor(pymysql.cursors.SSCursor)
        self._start_read_only()
        cursor.execute(statement)
        columns = [
            ResultColumn(str(d[0]), FIELD_TYPE_NAMES.get(d[1])) for d in (cursor.description or [])
        ]
        fetched = list(cursor.fetchmany(CONSOLE_MAX_ROWS + 1)) if cursor.description else []
        if len(fetched) > CONSOLE_MAX_ROWS:
            # Unread rows are left on the wire: dropping the connection stops the server-side
            # work immediately, where closing the cursor would first download all of them.
            self._drop_connection()
        return columns, fetched

    # --- plumbing -----------------------------------------------------------------

    def _drop_connection(self) -> None:
        with contextlib.suppress(AttributeError):
            self._conn._result.unbuffered_active = False  # nothing left to drain on close
        self._conn._force_close()
        self._dead = True

    def _start_read_only(self) -> None:
        with self._conn.cursor() as cursor:
            cursor.execute("START TRANSACTION READ ONLY")

    def _fetch(self, query: str, params: Sequence[object] = ()) -> list[Row]:
        started = time.monotonic()
        try:
            self._start_read_only()
            with self._conn.cursor() as cursor:
                cursor.execute(query, tuple(params))
                return list(cursor.fetchall())
        except pymysql.Error as error:
            raise _map_error(error, self._spec, started=started) from error
        finally:
            self._rollback()

    def _rollback(self) -> None:
        if not self._dead:
            with contextlib.suppress(pymysql.Error):
                self._conn.rollback()


def _connect(spec: ConnectionSpec, address: str, limits: Limits) -> Any:
    tls: ssl.SSLContext | None = None
    if spec.tls is TlsMode.REQUIRE:
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        tls.check_hostname = False
        tls.verify_mode = ssl.CERT_NONE
    try:
        return pymysql.connect(
            host=address,
            port=spec.port,
            user=spec.username,
            password=spec.password,
            database=spec.database,
            charset="utf8mb4",
            connect_timeout=limits.connect_timeout_s,
            read_timeout=limits.statement_timeout_ms / 1000 + 5,
            write_timeout=10,
            autocommit=False,
            ssl=tls,
            ssl_disabled=tls is None,
            program_name="jendock-inspector",
        )
    except (pymysql.Error, RuntimeError) as error:
        message = _clean(error.args[1] if len(error.args) > 1 else str(error), spec)
        raise InspectorError(
            ErrorCode.CONNECT_FAILED, message or "cannot connect to the database"
        ) from error


def _clean(message: object, spec: ConnectionSpec) -> str:
    text = str(message).strip().splitlines()[0] if str(message).strip() else ""
    if spec.password:
        text = text.replace(spec.password, "***")
    return text[:ERROR_TEXT_LIMIT]


def _map_error(error: pymysql.Error, spec: ConnectionSpec, *, started: float) -> InspectorError:
    code = error.args[0] if error.args and isinstance(error.args[0], int) else 0
    if code in TIMEOUT_ERRORS:
        return InspectorError(ErrorCode.TIMEOUT, "statement exceeded the time limit")
    if code in CONNECTION_LOST and time.monotonic() - started >= 1:
        return InspectorError(ErrorCode.TIMEOUT, "statement exceeded the time limit")
    message = _clean(error.args[1] if len(error.args) > 1 else str(error), spec)
    return InspectorError(ErrorCode.QUERY_FAILED, message or "query failed")


def _mode(data_type: str) -> Mode:
    if data_type in NATIVE_TYPES:
        return Mode.NATIVE
    if data_type in BINARY_TYPES:
        return Mode.BINARY
    return Mode.TEXT


def _plan(columns: list[tuple[ColumnInfo, str]]) -> PagePlan:
    select = [SelectColumn(c.name, c.data_type, _mode(dt), dt) for c, dt in columns]
    keys = sorted(
        (i for i, (c, _dt) in enumerate(columns) if c.primary_key_position is not None),
        key=lambda i: columns[i][0].primary_key_position or 0,
    )
    if keys:
        return PagePlan(select, keys, [select[i].name for i in keys], ORDERING_PRIMARY_KEY)
    sortable = [c.name for c, dt in columns if dt in SORTABLE_TYPES]
    return PagePlan(select, [], sortable, ORDERING_COLUMNS if sortable else ORDERING_NONE)


def _select_expression(column: SelectColumn) -> str:
    data_type = column.raw_type
    name = quote_identifier(column.name)
    if column.mode is Mode.NATIVE:
        return name
    if column.mode is Mode.BINARY:
        return f"SUBSTRING({name}, 1, 64)"
    if data_type in GEOMETRY_TYPES:
        return f"LEFT(ST_AsText({name}), {TEXT_FETCH_CHARS})"
    if data_type in {"char", "varchar", "tinytext", "text", "mediumtext", "longtext"}:
        return f"LEFT({name}, {TEXT_FETCH_CHARS})"
    return f"LEFT(CAST({name} AS CHAR), {TEXT_FETCH_CHARS})"


def _page_query(
    schema: str, table: str, plan: PagePlan, request: RowRequest
) -> tuple[str, tuple[object, ...]]:
    expressions = [_select_expression(c) for c in plan.select]
    expressions += [
        f"LENGTH({quote_identifier(c.name)})" for c in plan.select if c.mode is Mode.BINARY
    ]
    query = (
        f"SELECT {', '.join(expressions)} FROM {quote_identifier(schema)}.{quote_identifier(table)}"  # noqa: S608
    )
    params: list[object] = []
    if request.after is not None:
        keys = ", ".join(quote_identifier(n) for n in plan.order_names)
        marks = ", ".join(["%s"] * len(request.after))
        query += f" WHERE ({keys}) > ({marks})"
        params.extend(request.after)
    if plan.order_names:
        query += " ORDER BY " + ", ".join(quote_identifier(n) for n in plan.order_names)
    offset = 0 if request.after is not None else request.offset
    query += " LIMIT %s OFFSET %s"
    params.extend([request.limit + 1, offset])
    return query, tuple(params)
