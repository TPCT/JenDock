"""MCP database tools against the real worker RPC with the inspector helper faked."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from shiftcodes.api.container import AppContainer
from shiftcodes.api.principal import Principal
from shiftcodes.api.schemas.databases import decode_cursor
from shiftcodes.domain.actor import Actor
from shiftcodes.domain.database import DatabaseEngine
from shiftcodes.domain.enums import EnvironmentName, TokenScope
from shiftcodes.mcp.dispatcher import ToolDispatcher
from shiftcodes.mcp.tools import default_registry
from shiftcodes.services.audit import AuditService
from shiftcodes.services.database_connections import NewConnection
from tests import test_databases_api
from tests.conftest import DEV, PROD
from tests.inspector_support import PAGE, FakeHelper
from tests.test_mcp import _real_principal, _run, _session, _text, _token

wired = test_databases_api.wired  # API + real worker RPC + faked helper container
helper = test_databases_api.helper
PASSWORD = test_databases_api.PASSWORD

REGISTRY = default_registry()
DATA_TOOLS = {"read_table_rows", "run_readonly_query"}
METADATA_TOOLS = {
    "list_database_connections",
    "get_database_connection",
    "list_database_schemas",
    "list_database_tables",
    "get_database_table",
}
CONNECTION = {"connection": "main"}


def _target(env: str = "development") -> dict[str, object]:
    return {"slug": "demo", "environment": env, **CONNECTION}


def _table(env: str = "development") -> dict[str, object]:
    return {**_target(env), "schema": "public", "table": "users"}


@pytest.fixture(autouse=True)
def _connections(wired: AppContainer) -> None:
    """One connection per environment, created through the service (password into the store)."""
    service = wired.database_connections()
    for env in (DEV, PROD):
        new = NewConnection(
            name="main",
            engine=DatabaseEngine.POSTGRESQL,
            host="db",
            database="app",
            username="reader",
            password=PASSWORD,
        )
        service.create("demo", env, new, Actor.system("test"))


def _payload(result: object) -> dict[str, Any]:  # Any: decoded JSON of a tool result
    structured = getattr(result, "structured_content", None)
    assert isinstance(structured, dict)
    return structured


def _call(
    container: AppContainer, principal: Principal, name: str, arguments: Mapping[str, object]
) -> dict[str, Any]:
    return _payload(ToolDispatcher(REGISTRY, container).call(principal, name, arguments))


# --- registry and scopes ---------------------------------------------------------------------


def test_database_tools_have_the_rest_read_scope() -> None:
    for name in METADATA_TOOLS | DATA_TOOLS:
        tool = REGISTRY.get(name)
        assert tool is not None
        assert tool.scope is TokenScope.READ, name


def test_no_connection_management_tool_exists() -> None:
    names = {t.name for t in REGISTRY.all()}
    for banned in ("create_database", "update_database", "delete_database", "test_database"):
        assert not any(name.startswith(banned) for name in names), banned
    assert {n for n in names if "database" in n or "table" in n or "query" in n} == (
        METADATA_TOOLS | DATA_TOOLS
    )


def test_scope_filtering(wired: AppContainer) -> None:
    def visible(*scopes: TokenScope) -> set[str]:
        principal = _real_principal(wired, *scopes)
        return {t.name for t in REGISTRY.visible_to(principal)} & (METADATA_TOOLS | DATA_TOOLS)

    assert visible(TokenScope.READ) == METADATA_TOOLS | DATA_TOOLS
    assert visible(TokenScope.RUN) == set()
    assert visible(TokenScope.CONTAINERS) == set()
    assert visible(TokenScope.ADMIN) == METADATA_TOOLS | DATA_TOOLS


# --- connections and metadata ----------------------------------------------------------------


def test_connections_are_listed_without_secrets(wired: AppContainer) -> None:
    reader = _real_principal(wired, TokenScope.READ)
    listing = {"slug": "demo", "environment": "development"}
    [item] = _call(wired, reader, "list_database_connections", listing)["items"]
    assert item["name"] == "main"
    assert item["password_secret"] == "DBCONN_MAIN_PASSWORD"  # a name, not a value
    assert PASSWORD not in json.dumps(item)
    one = _call(wired, reader, "get_database_connection", _target())
    assert one["database"] == "app"
    missing = _call(wired, reader, "get_database_connection", {**_target(), "connection": "nope"})
    assert missing["error"]["code"] == "not_found"


def test_metadata_tools_return_the_rest_shapes(wired: AppContainer, helper: FakeHelper) -> None:
    reader = _real_principal(wired, TokenScope.READ)
    schemas = _call(wired, reader, "list_database_schemas", {**_target(), "include_system": True})
    assert "schemas" in schemas
    assert helper.requests[-1][1].args.get("include_system") is True
    tables = _call(wired, reader, "list_database_tables", {**_target(), "schema": "public"})
    assert "tables" in tables
    detail = _call(wired, reader, "get_database_table", _table())
    assert {"columns", "indexes"} <= set(detail)


def test_production_metadata_needs_only_read(wired: AppContainer) -> None:
    reader = _real_principal(wired, TokenScope.READ)
    answer = _call(wired, reader, "list_database_tables", {**_target("production"), "schema": "x"})
    assert "error" not in answer


# --- dev versus production data --------------------------------------------------------------


def test_development_rows_and_console_need_only_read(wired: AppContainer) -> None:
    reader = _real_principal(wired, TokenScope.READ)
    rows = _call(wired, reader, "read_table_rows", {**_table(), "limit": 2})
    assert rows["rows"] == PAGE["rows"]
    console = _call(
        wired, reader, "run_readonly_query", {**_target(), "statement": "SELECT 7777 AS n"}
    )
    assert console["rows"] == [[7777]]


def test_production_rows_and_console_need_admin(wired: AppContainer, helper: FakeHelper) -> None:
    reader = _real_principal(wired, TokenScope.READ)
    before = len(helper.requests)
    denied_rows = _call(wired, reader, "read_table_rows", _table("production"))
    assert denied_rows["error"]["code"] == "forbidden"
    denied_query = _call(
        wired, reader, "run_readonly_query", {**_target("production"), "statement": "SELECT 1"}
    )
    assert denied_query["error"]["code"] == "forbidden"
    assert len(helper.requests) == before  # the helper container was never started
    admin = _real_principal(wired, TokenScope.ADMIN)
    assert "rows" in _call(wired, admin, "read_table_rows", _table("production"))
    assert "rows" in _call(
        wired, admin, "run_readonly_query", {**_target("production"), "statement": "SELECT 1"}
    )


def test_rows_use_an_opaque_cursor_and_bad_paging_is_refused(
    wired: AppContainer, helper: FakeHelper
) -> None:
    reader = _real_principal(wired, TokenScope.READ)
    first = _call(wired, reader, "read_table_rows", {**_table(), "limit": 2})
    assert decode_cursor(first["next_cursor"]) == [2]
    _call(
        wired, reader, "read_table_rows", {**_table(), "limit": 2, "cursor": first["next_cursor"]}
    )
    assert helper.requests[-1][1].args["after"] == [2]
    for bad in ({"limit": 0}, {"limit": 501}, {"offset": -1}):
        answer = _call(wired, reader, "read_table_rows", {**_table(), **bad})
        assert answer["error"]["code"] == "invalid_input"
    garbage = _call(wired, reader, "read_table_rows", {**_table(), "cursor": "!!!"})
    assert garbage["error"]["code"] == "validation_error"


def test_writes_are_refused_by_the_console(wired: AppContainer) -> None:
    reader = _real_principal(wired, TokenScope.READ)
    answer = _call(
        wired, reader, "run_readonly_query", {**_target(), "statement": "DROP TABLE users"}
    )
    assert "error" in answer


# --- audit -----------------------------------------------------------------------------------


def test_audit_has_the_statement_and_never_results_or_the_password(wired: AppContainer) -> None:
    reader = _real_principal(wired, TokenScope.READ)
    _call(wired, reader, "run_readonly_query", {**_target(), "statement": "SELECT 7777 AS n"})
    _call(wired, reader, "read_table_rows", _table("production"))  # refused
    events = AuditService(wired.uow(), wired.clock).list(limit=100)
    mcp = [e for e in events if e.action == "mcp.tool_call"]
    assert [(e.details["tool"], e.details["outcome"]) for e in reversed(mcp)] == [
        ("run_readonly_query", "ok"),
        ("read_table_rows", "error"),
    ]
    assert reader.token is not None
    assert {(e.actor_type.value, e.actor_id) for e in mcp} == {("token", str(reader.token.id))}
    assert mcp[-1].details["arguments"]["statement"] == "SELECT 7777 AS n"  # type: ignore[index]
    queries = [e for e in events if e.action == "database.query"]
    assert queries  # the service audits the console statement itself
    assert queries[0].actor_id == str(reader.token.id)
    text = json.dumps([e.details for e in events])
    assert PASSWORD not in text
    assert "[[7777]]" not in text  # the result rows are never audited (only a count is)
    assert EnvironmentName.PRODUCTION.value in text


# --- end to end with the SDK client ----------------------------------------------------------


def test_a_client_session_reads_metadata_and_development_rows_but_not_production(
    wired: AppContainer,
) -> None:
    token = _token(wired, TokenScope.READ)

    async def scenario() -> None:
        async with _session(wired, token) as session:
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert names >= METADATA_TOOLS | DATA_TOOLS
            listed = await session.call_tool(
                "list_database_connections", {"slug": "demo", "environment": "development"}
            )
            assert not listed.is_error
            assert _text(listed)["items"][0]["name"] == "main"
            assert PASSWORD not in str(_text(listed))
            rows = await session.call_tool("read_table_rows", {**_table(), "limit": 2})
            assert not rows.is_error
            assert _text(rows)["rows"] == PAGE["rows"]
            denied = await session.call_tool("read_table_rows", _table("production"))
            assert denied.is_error
            assert _text(denied)["error"]["code"] == "forbidden"

    _run(scenario())
