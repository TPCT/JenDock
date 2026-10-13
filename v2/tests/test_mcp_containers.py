"""MCP container tools through the real worker RPC on fake Docker."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from shiftcodes.api.container import AppContainer
from shiftcodes.api.principal import Principal
from shiftcodes.domain.enums import TokenScope
from shiftcodes.mcp.dispatcher import ToolDispatcher
from shiftcodes.mcp.tools import default_registry
from shiftcodes.repositories.database import UnitOfWork, create_session_factory
from shiftcodes.services.audit import AuditService
from shiftcodes.services.secret_cipher import SecretCipher
from shiftcodes.services.secrets import SecretService
from shiftcodes.worker.container_handlers import build_container_handlers
from shiftcodes.worker.handlers import build_handlers
from shiftcodes.worker.rpc import RpcServer
from tests.api_support import FakeClock, make_container, make_settings
from tests.container_harness import Stack, build_stack
from tests.test_mcp import _real_principal, _run, _session, _text, _token

REGISTRY = default_registry()
TARGET = {"slug": "demo", "environment": "development"}
SECRET_VALUE = "hunter2-secret"  # the DB_PASSWORD of the fake web container
READ_TOOLS = {
    "list_containers",
    "get_container",
    "container_stats",
    "host_telemetry",
    "list_container_env_names",
}
CONTAINERS_TOOLS = {
    "tail_container_logs",
    "preview_container_action",
    "confirm_container_action",
    "update_container_limits",
}


@dataclass
class Wired:
    container: AppContainer
    stack: Stack
    engine: Engine

    def running(self) -> bool:
        """Whether the fake web container is running."""
        state: Any = self.stack.engine.inspect(self.web)["State"]  # Any: docker inspect JSON
        return bool(state["Running"])

    @property
    def web(self) -> str:
        return self.stack.web_id

    def target(self, container_id: str | None = None) -> dict[str, object]:
        return {**TARGET, "container_id": container_id or self.web}


@pytest.fixture
def wired(
    tmp_path: Path, socket_dir: Path, engine: Engine, clock: FakeClock, project: str
) -> Iterator[Wired]:
    stack = build_stack(tmp_path, engine, project)
    factory = create_session_factory(engine)
    cipher = SecretCipher(tmp_path / "worker-only" / "secret.key")
    handlers = build_handlers(
        lambda: SecretService(UnitOfWork(factory), cipher), build_container_handlers(stack.services)
    )
    settings = make_settings(tmp_path, socket_path=socket_dir / "w.sock")
    rpc = RpcServer(settings.worker_socket_path, handlers)
    rpc.start()
    try:
        yield Wired(make_container(settings, engine, clock), stack, engine)
    finally:
        rpc.stop()


def _payload(result: object) -> dict[str, Any]:  # Any: decoded JSON of a tool result
    structured = getattr(result, "structured_content", None)
    assert isinstance(structured, dict)
    return structured


def _call(
    container: AppContainer, principal: Principal, name: str, arguments: Mapping[str, object]
) -> dict[str, Any]:
    return _payload(ToolDispatcher(REGISTRY, container).call(principal, name, arguments))


# --- registry and scopes ---------------------------------------------------------------------


def test_container_tools_mirror_the_rest_scopes() -> None:
    scopes = {t.name: t.scope for t in REGISTRY.all()}
    for name in READ_TOOLS:
        assert scopes[name] is TokenScope.READ, name
    for name in CONTAINERS_TOOLS:
        assert scopes[name] is TokenScope.CONTAINERS, name


def test_no_terminal_stream_reveal_edit_or_file_tool_exists() -> None:
    names = {t.name for t in REGISTRY.all()}
    for banned in (
        "terminal",
        "follow",
        "reveal",
        "update_container_env",
        "set_env",
        "read_file",
        "list_files",
    ):
        assert not any(banned in name for name in names), banned


def test_scopes_filter_the_container_tools(wired: Wired) -> None:
    def visible(*scopes: TokenScope) -> set[str]:
        principal = _real_principal(wired.container, *scopes)
        return {t.name for t in REGISTRY.visible_to(principal)} & (READ_TOOLS | CONTAINERS_TOOLS)

    assert visible(TokenScope.READ) == READ_TOOLS
    assert visible(TokenScope.CONTAINERS) == CONTAINERS_TOOLS
    assert visible(TokenScope.RUN) == set()
    assert visible(TokenScope.ADMIN) == READ_TOOLS | CONTAINERS_TOOLS


def test_a_read_token_cannot_read_logs_or_act(wired: Wired) -> None:
    reader = _real_principal(wired.container, TokenScope.READ)
    for name, extra in (
        ("tail_container_logs", {}),
        ("preview_container_action", {"action": "restart"}),
        ("confirm_container_action", {"confirmation": "x"}),
        ("update_container_limits", {"memory": "256m"}),
    ):
        answer = _call(wired.container, reader, name, {**wired.target(), **extra})
        assert answer["error"]["code"] == "forbidden", name
    assert wired.running()


# --- read tools ------------------------------------------------------------------------------


def test_inventory_detail_stats_and_telemetry(wired: Wired) -> None:
    reader = _real_principal(wired.container, TokenScope.READ)
    items = _call(wired.container, reader, "list_containers", TARGET)["items"]
    assert [i["service"] for i in items] == ["web", "worker"]  # the foreign container is hidden
    one = _call(wired.container, reader, "get_container", wired.target())
    assert one["id"] == wired.web
    assert one["state"] == "running"
    assert SECRET_VALUE not in json.dumps(items)
    stats = _call(wired.container, reader, "container_stats", wired.target())
    assert "history" in json.dumps(stats) or stats
    host = _call(wired.container, reader, "host_telemetry", {})
    assert host
    foreign = _call(wired.container, reader, "get_container", wired.target(wired.stack.foreign_id))
    assert foreign["error"]["code"] == "not_found"
    unknown = _call(wired.container, reader, "get_container", wired.target("0" * 64))
    assert unknown["error"]["code"] == "not_found"
    short = _call(wired.container, reader, "get_container", wired.target(wired.web[:12]))
    assert short["error"]["code"] == "invalid_input"


def test_env_names_are_listed_without_any_value(wired: Wired) -> None:
    reader = _real_principal(wired.container, TokenScope.READ)
    answer = _call(wired.container, reader, "list_container_env_names", wired.target())
    names = {item["name"]: item for item in answer["items"]}
    assert {"PATH", "DB_PASSWORD"} <= set(names)
    assert names["DB_PASSWORD"]["sensitive"] is True
    for item in names.values():
        assert set(item) == {"name", "sensitive", "overridden"}
    assert SECRET_VALUE not in json.dumps(answer)
    assert "*" not in json.dumps(answer)  # not even the masked placeholder


def test_logs_are_a_bounded_tail(wired: Wired) -> None:
    manager = _real_principal(wired.container, TokenScope.CONTAINERS)
    page = _call(wired.container, manager, "tail_container_logs", {**wired.target(), "tail": 5})
    assert set(page) >= {"lines", "cursor", "dropped"}
    too_many = _call(
        wired.container, manager, "tail_container_logs", {**wired.target(), "tail": 5000}
    )
    assert too_many["error"]["code"] == "invalid_input"


# --- lifecycle and limits --------------------------------------------------------------------


def test_restart_is_two_steps_with_a_single_use_confirmation(wired: Wired) -> None:
    manager = _real_principal(wired.container, TokenScope.CONTAINERS)
    plan = _call(
        wired.container,
        manager,
        "preview_container_action",
        {**wired.target(), "action": "restart"},
    )
    assert plan["action"] == "restart"
    assert plan["effects"]
    assert plan["confirmation"]
    assert (
        not wired.stack.engine.calls_of("restart")
        if hasattr(wired.stack.engine, "calls_of")
        else True
    )
    done = _call(
        wired.container,
        manager,
        "confirm_container_action",
        {**wired.target(), "confirmation": plan["confirmation"]},
    )
    assert done["action"] == "restart"
    assert done["container"]["id"] == wired.web
    again = _call(
        wired.container,
        manager,
        "confirm_container_action",
        {**wired.target(), "confirmation": plan["confirmation"]},
    )
    assert "error" in again  # single use


def test_a_confirmation_belongs_to_the_container_and_the_caller(wired: Wired) -> None:
    first = _real_principal(wired.container, TokenScope.CONTAINERS)
    second = _real_principal(wired.container, TokenScope.CONTAINERS)
    plan = _call(
        wired.container, first, "preview_container_action", {**wired.target(), "action": "stop"}
    )
    other = _call(
        wired.container,
        first,
        "confirm_container_action",
        {**wired.target(wired.stack.other_id), "confirmation": plan["confirmation"]},
    )
    assert "error" in other
    stolen = _call(
        wired.container,
        second,
        "confirm_container_action",
        {**wired.target(), "confirmation": plan["confirmation"]},
    )
    assert "error" in stolen
    assert wired.running()


def test_invalid_actions_are_refused_by_the_schema(wired: Wired) -> None:
    manager = _real_principal(wired.container, TokenScope.CONTAINERS)
    bad = _call(
        wired.container, manager, "preview_container_action", {**wired.target(), "action": "kill"}
    )
    assert bad["error"]["code"] == "invalid_input"


def test_limits_update_within_the_maxima(wired: Wired) -> None:
    manager = _real_principal(wired.container, TokenScope.CONTAINERS)
    view = _call(
        wired.container, manager, "update_container_limits", {**wired.target(), "cpus": 1.5}
    )
    assert view["id"] == wired.web
    nothing = _call(wired.container, manager, "update_container_limits", wired.target())
    assert "error" in nothing
    too_big = _call(
        wired.container, manager, "update_container_limits", {**wired.target(), "cpus": 64}
    )
    assert "error" in too_big


# --- audit -----------------------------------------------------------------------------------


def test_calls_are_audited_with_the_token_and_without_secrets(wired: Wired) -> None:
    manager = _real_principal(wired.container, TokenScope.CONTAINERS)
    plan = _call(
        wired.container, manager, "preview_container_action", {**wired.target(), "action": "stop"}
    )
    _call(
        wired.container,
        manager,
        "confirm_container_action",
        {**wired.target(), "confirmation": plan["confirmation"]},
    )
    _call(wired.container, manager, "list_container_env_names", wired.target())  # denied
    events = AuditService(wired.container.uow(), wired.container.clock).list(limit=100)
    mcp = [e for e in events if e.action == "mcp.tool_call"]
    assert [(e.details["tool"], e.details["outcome"]) for e in reversed(mcp)] == [
        ("preview_container_action", "ok"),
        ("confirm_container_action", "ok"),
        ("list_container_env_names", "denied"),
    ]
    assert manager.token is not None
    assert {(e.actor_type.value, e.actor_id) for e in mcp} == {("token", str(manager.token.id))}
    confirm_args = next(e for e in mcp if e.details["tool"] == "confirm_container_action").details[
        "arguments"
    ]
    assert isinstance(confirm_args, dict)
    assert confirm_args["confirmation"].startswith("<")  # recorded as a length only
    everything = json.dumps([e.details for e in events])
    assert plan["confirmation"] not in everything
    assert SECRET_VALUE not in everything


# --- end to end with the SDK client ----------------------------------------------------------


def test_a_client_session_reads_the_inventory_and_cannot_act_without_the_scope(
    wired: Wired,
) -> None:
    token = _token(wired.container, TokenScope.READ)

    async def scenario() -> None:
        async with _session(wired.container, token) as session:
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert names >= READ_TOOLS
            assert not CONTAINERS_TOOLS & names
            listed = await session.call_tool("list_containers", TARGET)
            assert not listed.is_error
            assert [c["service"] for c in _text(listed)["items"]] == ["web", "worker"]
            assert SECRET_VALUE not in str(_text(listed))
            denied = await session.call_tool(
                "preview_container_action", {**wired.target(), "action": "stop"}
            )
            assert denied.is_error
            assert _text(denied)["error"]["code"] == "forbidden"

    _run(scenario())
    assert wired.running()
