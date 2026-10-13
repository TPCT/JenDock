"""The inspector adapters against real PostgreSQL 16, MySQL 8.4 and MariaDB 11.4 servers.

Run with ``make test-docker``. Servers are throwaway containers labelled ``shiftcodes.test=1``
that publish on 127.0.0.1 only; they are removed when the module ends.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any, cast

import pytest

from shiftcodes.inspector.model import (
    Engine,
    ErrorCode,
    InspectorError,
    Limits,
    RowRequest,
)
from shiftcodes.inspector.operations import InspectorAdapter, open_adapter
from tests.docker_support import (
    assert_nothing_left,
    docker_available,
    remove_test_resources,
)
from tests.inspector_db_support import (
    BINARY_LENGTH,
    HOSTILE_COLUMNS_MYSQL,
    HOSTILE_COLUMNS_PG,
    HOSTILE_MYSQL,
    HOSTILE_PG,
    ITEM_COUNT,
    RunningDb,
    connect_admin,
    seed,
    start_server,
    stop_server,
)

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not docker_available(), reason="no Docker daemon available"),
]
ENGINES = [Engine.POSTGRESQL, Engine.MYSQL, Engine.MARIADB]


@pytest.fixture(scope="module", params=ENGINES, ids=[e.value for e in ENGINES])
def db(request: pytest.FixtureRequest) -> Iterator[RunningDb]:
    server = start_server(request.param)
    try:
        seed(server)
        yield server
    finally:
        stop_server(server)
        remove_test_resources()
        assert_nothing_left()


@pytest.fixture
def adapter(db: RunningDb) -> Iterator[InspectorAdapter]:
    session = open_adapter(db.spec(), "127.0.0.1", Limits(statement_timeout_ms=10_000))
    yield session
    session.close()


@pytest.fixture
def console(db: RunningDb) -> Iterator[InspectorAdapter]:
    session = open_adapter(db.spec(), "127.0.0.1", Limits(statement_timeout_ms=5_000))
    yield session
    session.close()


def _hostile_name(db: RunningDb) -> str:
    return HOSTILE_PG if db.engine is Engine.POSTGRESQL else HOSTILE_MYSQL


def _scalar(db: RunningDb, query: str) -> Any:
    connection = connect_admin(db)
    try:
        cursor = connection.cursor()
        cursor.execute(query)
        row = cursor.fetchone()
        return row[0] if row else None
    finally:
        connection.close()


# --- connection and read-only session ---------------------------------------------------


def test_server_info_reports_a_read_only_session(adapter: InspectorAdapter, db: RunningDb) -> None:
    info = adapter.server_info()
    assert info.read_only is True
    assert info.current_user.startswith("postgres" if db.engine is Engine.POSTGRESQL else "root")
    assert info.version
    assert info.tls is False


def test_wrong_password_is_a_connect_failure_without_the_password(db: RunningDb) -> None:
    spec = db.spec()
    bad = type(spec)(
        engine=spec.engine,
        host=spec.host,
        port=spec.port,
        database=spec.database,
        username=spec.username,
        password="not-the-password-xyz",
        tls=spec.tls,
    )
    with pytest.raises(InspectorError) as raised:
        open_adapter(bad, "127.0.0.1", Limits())
    assert raised.value.code is ErrorCode.CONNECT_FAILED
    assert "not-the-password-xyz" not in raised.value.message


# --- metadata ------------------------------------------------------------------------------


def test_schemas_hide_system_schemas_by_default(adapter: InspectorAdapter) -> None:
    names = [s.name for s in adapter.schemas()]
    assert "shop" in names
    assert "information_schema" not in names
    assert "information_schema" in [s.name for s in adapter.schemas(include_system=True)]


def test_tables_have_kinds_and_approximate_row_counts(
    adapter: InspectorAdapter, db: RunningDb
) -> None:
    tables = {t.name: t for t in adapter.tables("shop")}
    assert {"items", "composite", "orders", "nopk", "item_names", _hostile_name(db)} <= set(tables)
    assert tables["item_names"].kind == "view"
    assert tables["items"].kind == "table"
    estimate = tables["items"].approx_rows
    assert estimate is not None
    assert ITEM_COUNT * 0.5 <= estimate <= ITEM_COUNT * 1.5


def test_unknown_schema_and_table_are_not_found(adapter: InspectorAdapter) -> None:
    with pytest.raises(InspectorError) as schema:
        adapter.tables("nope")
    assert schema.value.code is ErrorCode.NOT_FOUND
    with pytest.raises(InspectorError) as table:
        adapter.table_detail("shop", "nope")
    assert table.value.code is ErrorCode.NOT_FOUND


def test_table_detail_columns_keys_indexes_and_foreign_keys(adapter: InspectorAdapter) -> None:
    items = adapter.table_detail("shop", "items")
    columns = {c.name: c for c in items.columns}
    assert [c.name for c in items.columns][:3] == ["id", "name", "note"]
    assert columns["id"].primary_key_position == 1
    assert columns["id"].nullable is False
    assert columns["name"].nullable is False
    assert columns["name"].default is not None
    assert "unnamed" in columns["name"].default
    assert columns["note"].nullable is True
    assert columns["note"].default is None
    assert any(i.primary and i.columns == ["id"] and i.unique for i in items.indexes)
    assert any(i.name == "items_name_idx" and i.columns == ["name"] for i in items.indexes)
    orders = adapter.table_detail("shop", "orders")
    assert len(orders.foreign_keys) == 1
    fk = orders.foreign_keys[0]
    assert fk.columns == ["item_id"]
    assert (fk.ref_table, fk.ref_columns) == ("items", ["id"])
    assert fk.on_delete == "CASCADE"
    assert fk.on_update == "RESTRICT"
    composite = adapter.table_detail("shop", "composite")
    assert {c.name: c.primary_key_position for c in composite.columns} == {
        "a": 1,
        "b": 2,
        "v": None,
    }


def test_expression_index_is_listed_on_postgres(adapter: InspectorAdapter, db: RunningDb) -> None:
    if db.engine is not Engine.POSTGRESQL:
        pytest.skip("expression indexes are checked on PostgreSQL")
    indexes = {i.name: i for i in adapter.table_detail("shop", "items").indexes}
    assert indexes["items_lower_idx"].unique is True
    assert indexes["items_lower_idx"].columns == ["lower(name::text)"]


# --- rows ----------------------------------------------------------------------------------


def test_offset_paging_is_stable_and_capped(adapter: InspectorAdapter) -> None:
    first = adapter.rows("shop", "items", RowRequest(limit=500))
    assert len(first.rows) == 500
    assert first.ordering == "primary_key"
    assert first.order_by == ["id"]
    assert first.next_offset == 500
    assert [r[0] for r in first.rows[:3]] == [1, 2, 3]
    second = adapter.rows("shop", "items", RowRequest(limit=500, offset=first.next_offset or 0))
    assert second.rows[0][0] == 501
    last = adapter.rows("shop", "items", RowRequest(limit=500, offset=1000))
    assert len(last.rows) == ITEM_COUNT - 1000
    assert last.next_offset is None
    assert last.next_after is None


@pytest.mark.parametrize("limit", [0, 501, -1])
def test_limit_outside_1_to_500_is_rejected(adapter: InspectorAdapter, limit: int) -> None:
    with pytest.raises(InspectorError) as raised:
        adapter.rows("shop", "items", RowRequest(limit=limit))
    assert raised.value.code is ErrorCode.BAD_REQUEST


def test_keyset_paging_walks_every_row_exactly_once(adapter: InspectorAdapter) -> None:
    seen: list[int] = []
    request = RowRequest(limit=450)
    for _ in range(10):
        page = adapter.rows("shop", "items", request)
        seen += [cast(int, r[0]) for r in page.rows]
        if page.next_after is None:
            break
        request = RowRequest(limit=450, after=page.next_after)
    assert seen == list(range(1, ITEM_COUNT + 1))


def test_keyset_paging_with_composite_primary_key(adapter: InspectorAdapter) -> None:
    seen: list[tuple[int, int]] = []
    request = RowRequest(limit=7)
    while True:
        page = adapter.rows("shop", "composite", request)
        seen += [(cast(int, r[0]), cast(int, r[1])) for r in page.rows]
        if page.next_after is None:
            break
        request = RowRequest(limit=7, after=page.next_after)
    assert seen == [(a, b) for a in range(1, 6) for b in range(1, 6)]


def test_keyset_cursor_must_match_the_primary_key(adapter: InspectorAdapter) -> None:
    with pytest.raises(InspectorError):
        adapter.rows("shop", "composite", RowRequest(after=[1]))
    with pytest.raises(InspectorError):
        adapter.rows("shop", "nopk", RowRequest(after=[1]))


def test_table_without_primary_key_is_ordered_by_its_columns(adapter: InspectorAdapter) -> None:
    page = adapter.rows("shop", "nopk", RowRequest())
    assert page.ordering == "columns"
    assert [r[0] for r in page.rows] == [1, 2, 3]
    assert page.next_after is None


def test_long_text_is_truncated_to_4_kib_and_binary_is_a_hex_preview(
    adapter: InspectorAdapter,
) -> None:
    page = adapter.rows("shop", "items", RowRequest(limit=10))
    names = [c.name for c in page.columns]
    row = page.rows[6]
    assert row[0] == 7
    note = row[names.index("note")]
    assert isinstance(note, dict)
    assert note["truncated"] is True
    assert note["limit"] == 4096
    assert note["text"] == "x" * 4096
    payload = row[names.index("payload")]
    assert payload == {"binary": True, "hex": bytes(range(64)).hex(), "bytes": BINARY_LENGTH}
    assert page.rows[0][names.index("payload")] is None
    assert page.rows[0][names.index("note")] == "n1"
    assert page.rows[9][names.index("note")] is None


def test_text_cut_never_splits_a_multibyte_character(db: RunningDb) -> None:
    connection = connect_admin(db)
    try:
        cursor = connection.cursor()
        cursor.execute("CREATE TABLE shop.unicode (id int PRIMARY KEY, t text)")
        cursor.execute("INSERT INTO shop.unicode VALUES (1, %s)", ("é" * 3000,))
    finally:
        connection.close()
    session = open_adapter(db.spec(), "127.0.0.1", Limits())
    try:
        cell = session.rows("shop", "unicode", RowRequest()).rows[0][1]
    finally:
        session.close()
    assert isinstance(cell, dict)
    assert cell["text"] == "é" * 2048
    assert len(cell["text"].encode()) == 4096


def test_hostile_table_and_column_names_are_quoted(
    adapter: InspectorAdapter, db: RunningDb
) -> None:
    table = _hostile_name(db)
    detail = adapter.table_detail("shop", table)
    expected = HOSTILE_COLUMNS_PG if db.engine is Engine.POSTGRESQL else HOSTILE_COLUMNS_MYSQL
    assert [c.name for c in detail.columns] == ["id", *expected]
    page = adapter.rows("shop", table, RowRequest())
    assert [c.name for c in page.columns] == ["id", *expected]
    assert page.rows == [[1, 10, 20, 30]]
    keyset = adapter.rows("shop", table, RowRequest(after=[0]))
    assert keyset.rows == [[1, 10, 20, 30]]
    # The statement text inside the name never ran: the real table is intact.
    assert adapter.rows("shop", "items", RowRequest(limit=1)).rows[0][0] == 1


def test_hostile_values_in_the_cursor_are_bound_not_executed(adapter: InspectorAdapter) -> None:
    try:
        page = adapter.rows("shop", "items", RowRequest(limit=1, after=["1; DROP TABLE items; --"]))
        assert page.rows == [] or cast(int, page.rows[0][0]) >= 1
    except InspectorError as error:  # PostgreSQL refuses the text as an integer
        assert error.code is ErrorCode.QUERY_FAILED
    assert adapter.tables("shop")
    assert adapter.rows("shop", "items", RowRequest(limit=1)).rows[0][0] == 1


# --- console -----------------------------------------------------------------------------


def test_console_runs_select_with_show_and_explain(
    console: InspectorAdapter, db: RunningDb
) -> None:
    result = console.query("SELECT id, name FROM shop.items WHERE id <= 3 ORDER BY id;")
    assert [c.name for c in result.columns] == ["id", "name"]
    assert result.rows == [[1, "item-0001"], [2, "item-0002"], [3, "item-0003"]]
    assert result.truncated is False
    assert result.statement_kind == "SELECT"
    cte = console.query("WITH t AS (SELECT 1 AS one) SELECT one FROM t")
    assert cte.rows == [[1]] and cte.statement_kind == "WITH"
    show = console.query(
        "SHOW server_version" if db.engine is Engine.POSTGRESQL else "SHOW DATABASES"
    )
    assert show.row_count >= 1 and show.statement_kind == "SHOW"
    explain = console.query("EXPLAIN SELECT * FROM shop.items")
    assert explain.row_count >= 1 and explain.statement_kind == "EXPLAIN"


def test_console_percent_signs_in_literals_are_not_formatting(console: InspectorAdapter) -> None:
    result = console.query("SELECT count(*) FROM shop.items WHERE name LIKE 'item-000%'")
    assert result.rows == [[9]]


def test_console_limits_rows_to_500_and_says_so(console: InspectorAdapter) -> None:
    result = console.query("SELECT id FROM shop.items ORDER BY id")
    assert result.row_count == 500
    assert result.truncated is True
    assert result.limit == 500
    assert [r[0] for r in result.rows[:2]] == [1, 2]


def test_console_truncates_cells_and_shows_binary_as_hex(console: InspectorAdapter) -> None:
    result = console.query("SELECT note, payload FROM shop.items WHERE id = 7")
    note, payload = result.rows[0]
    assert isinstance(note, dict) and note["truncated"] is True and len(note["text"]) == 4096
    assert isinstance(payload, dict) and payload["bytes"] == BINARY_LENGTH


@pytest.mark.parametrize(
    "statement",
    [
        "SELECT 1; SELECT 2",
        "SELECT 1; DROP TABLE shop.items",
        "INSERT INTO shop.nopk VALUES (9, 'z')",
        "UPDATE shop.nopk SET y = 'q'",
        "DELETE FROM shop.nopk",
        "DROP TABLE shop.nopk",
        "CREATE TABLE shop.evil (x int)",
        "TRUNCATE shop.nopk",
        "SELECT x INTO OUTFILE '/tmp/x' FROM shop.nopk",
        "SELECT * FROM shop.nopk FOR UPDATE",
        "SELECT pg_sleep(1)",
        "SELECT sleep(1)",
        "WITH d AS (DELETE FROM shop.nopk RETURNING *) SELECT * FROM d",
        "EXPLAIN ANALYZE DELETE FROM shop.nopk",
        "",
        "   -- only a comment",
    ],
)
def test_console_rejects_writes_chains_and_dangerous_functions(
    console: InspectorAdapter, statement: str
) -> None:
    with pytest.raises(InspectorError) as raised:
        console.query(statement)
    assert raised.value.code is ErrorCode.REJECTED
    assert _scalar_on(console, "SELECT count(*) FROM shop.nopk") == 3


def _scalar_on(console: InspectorAdapter, statement: str) -> Any:
    return console.query(statement).rows[0][0]


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO shop.nopk VALUES (9, 'z')",
        "UPDATE shop.nopk SET y = 'q'",
        "DELETE FROM shop.nopk",
        "CREATE TABLE shop.evil (x int)",
        "DROP TABLE shop.nopk",
        "TRUNCATE shop.nopk",
    ],
)
def test_database_itself_refuses_writes_when_the_parser_is_bypassed(
    db: RunningDb, console: InspectorAdapter, statement: str
) -> None:
    runner = cast(Any, console)
    with pytest.raises(InspectorError) as raised:
        runner.run_unchecked(statement)
    assert raised.value.code is ErrorCode.QUERY_FAILED
    assert "read" in raised.value.message.lower()
    assert _scalar(db, "SELECT count(*) FROM shop.nopk") == 3


def test_a_statement_that_bypasses_the_parser_cannot_leave_the_transaction(
    db: RunningDb, console: InspectorAdapter
) -> None:
    runner = cast(Any, console)
    for statement in ("COMMIT", "ROLLBACK", "SET TRANSACTION READ WRITE"):
        with pytest.raises(InspectorError):
            runner.run_unchecked(statement + "; INSERT INTO shop.nopk VALUES (9, 'z')")  # noqa: S608
    assert _scalar(db, "SELECT count(*) FROM shop.nopk") == 3


def test_chained_statements_that_bypass_the_parser_still_do_not_write(
    db: RunningDb, console: InspectorAdapter
) -> None:
    runner = cast(Any, console)
    for statement in (
        "SELECT 1; INSERT INTO shop.nopk VALUES (9, 'z')",
        "SELECT 1; DELETE FROM shop.nopk",
    ):
        with pytest.raises(InspectorError):
            runner.run_unchecked(statement)
    assert _scalar(db, "SELECT count(*) FROM shop.nopk") == 3


def test_console_statement_timeout_stops_a_runaway_query(db: RunningDb) -> None:
    session = open_adapter(db.spec(), "127.0.0.1", Limits(statement_timeout_ms=1_000))
    started = time.monotonic()
    try:
        with pytest.raises(InspectorError) as raised:
            session.query(
                "SELECT sum(a.id + b.id + c.id + d.id) FROM shop.items a, shop.items b, "
                "shop.items c, shop.items d"
            )
    finally:
        session.close()
    assert raised.value.code is ErrorCode.TIMEOUT
    assert time.monotonic() - started < 10


def test_console_reports_sql_errors_without_leaking_connection_details(
    console: InspectorAdapter,
) -> None:
    with pytest.raises(InspectorError) as raised:
        console.query("SELECT * FROM shop.no_such_table")
    assert raised.value.code is ErrorCode.QUERY_FAILED
    assert "no_such_table" in raised.value.message
