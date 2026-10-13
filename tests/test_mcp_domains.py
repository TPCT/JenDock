"""MCP domain tools against the real ingress worker RPC with fake Cloudflare, Apache and CA."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest
from fastapi import FastAPI

from backend.api.container import AppContainer
from backend.api.principal import Principal
from backend.domain.actor import Actor
from backend.domain.enums import TokenScope
from backend.mcp.dispatcher import ToolDispatcher
from backend.mcp.tools import default_registry
from backend.services.audit import AuditService
from tests import test_domains_api
from tests.ingress_support import HOST
from tests.test_domains_api import Api
from tests.test_mcp import _run, _session, _text, _token

api = test_domains_api.api  # the same fixture: API + worker RPC + fake Cloudflare/Apache/CA
REGISTRY = default_registry()
DOMAIN_TOOLS = {
    "list_domains",
    "get_domain",
    "list_certificates",
    "check_domain",
    "preview_domain_change",
    "apply_domain",
    "remove_domain",
    "issue_certificate",
}
READ_TOOLS = {"list_domains", "get_domain", "list_certificates"}
TARGET = {"slug": "demo", "environment": "development"}


def _container(api: Api) -> AppContainer:
    app: FastAPI = api.client.app._app  # type: ignore[attr-defined]  # inside the headers wrapper
    container: AppContainer = app.state.container
    return container


def _principal(container: AppContainer, *scopes: TokenScope) -> Principal:
    with container.uow() as uow:
        user = uow.users.get_by_username("admin")
        assert user is not None
        owner = user.id
    created = container.token_service().create(
        "agent", list(scopes), owner, None, Actor.system("t")
    )
    token = container.token_service().authenticate(created.value)
    assert token is not None
    return Principal(token=token)


def _payload(result: object) -> dict[str, Any]:  # Any: decoded JSON of a tool result
    structured = getattr(result, "structured_content", None)
    assert isinstance(structured, dict)
    return structured


def _call(
    container: AppContainer, principal: Principal, name: str, arguments: Mapping[str, object]
) -> dict[str, Any]:
    return _payload(ToolDispatcher(REGISTRY, container).call(principal, name, arguments))


# --- registry and scope filtering ------------------------------------------------------------


def test_domain_tools_are_registered_with_the_rest_scopes() -> None:
    assert {t.name for t in REGISTRY.all()} >= DOMAIN_TOOLS
    for name in DOMAIN_TOOLS:
        tool = REGISTRY.get(name)
        assert tool is not None
        expected = TokenScope.READ if name in READ_TOOLS else TokenScope.ADMIN
        assert tool.scope is expected, name
        assert tool.read_only == (name in READ_TOOLS)


def test_scopes_filter_the_domain_tools(api: Api) -> None:
    container = _container(api)
    visible = {
        scope: {t.name for t in REGISTRY.visible_to(_principal(container, scope))} & DOMAIN_TOOLS
        for scope in (TokenScope.READ, TokenScope.RUN, TokenScope.PIPELINES, TokenScope.ADMIN)
    }
    assert visible[TokenScope.READ] == READ_TOOLS
    assert visible[TokenScope.RUN] == set()
    assert visible[TokenScope.PIPELINES] == set()
    assert visible[TokenScope.ADMIN] == DOMAIN_TOOLS


def test_a_read_token_cannot_change_anything(api: Api) -> None:
    container = _container(api)
    domain_id = api.create()
    reader = _principal(container, TokenScope.READ)
    arguments = {**TARGET, "domain_id": domain_id}
    for name, extra in (
        ("check_domain", {}),
        ("preview_domain_change", {}),
        ("apply_domain", {"plan_hash": "a" * 64}),
        ("remove_domain", {"plan_hash": "a" * 64}),
        ("issue_certificate", {}),
    ):
        answer = _call(container, reader, name, {**arguments, **extra})
        assert answer["error"]["code"] == "forbidden", name
    assert api.world.cloudflare.mutations() == []
    assert api.world.helper.vhosts == {}


# --- the tools --------------------------------------------------------------------------------


def test_list_and_get_show_status_and_certificate_expiry(api: Api) -> None:
    container = _container(api)
    domain_id = api.create()
    reader = _principal(container, TokenScope.READ)
    admin = _principal(container, TokenScope.ADMIN)
    [draft] = _call(container, reader, "list_domains", TARGET)["items"]
    assert (draft["hostname"], draft["status"], draft["certificate"]) == (HOST, "draft", None)
    plan = _call(container, admin, "preview_domain_change", {**TARGET, "domain_id": domain_id})
    assert plan["operation"] == "apply"
    assert len(plan["plan_hash"]) == 64
    assert plan["dns"]["action"] == "create"
    assert "VirtualHost" in plan["vhost"]["diff"]
    assert api.world.cloudflare.mutations() == []  # a preview changes nothing
    applied = _call(
        container,
        admin,
        "apply_domain",
        {**TARGET, "domain_id": domain_id, "plan_hash": plan["plan_hash"]},
    )
    assert applied["blocked"] is False
    one = _call(container, reader, "get_domain", {**TARGET, "domain_id": domain_id})
    assert one["status"] == "active"
    assert one["certificate"]["state"] == "ok"
    assert one["certificate"]["days_remaining"] == 90
    overview = _call(container, reader, "list_certificates", {})["items"]
    assert [(c["hostname"], c["state"]) for c in overview] == [(HOST, "ok")]


def test_apply_refuses_a_plan_hash_that_was_not_previewed(api: Api) -> None:
    container = _container(api)
    domain_id = api.create()
    admin = _principal(container, TokenScope.ADMIN)
    answer = _call(
        container,
        admin,
        "apply_domain",
        {**TARGET, "domain_id": domain_id, "plan_hash": "f" * 64},
    )
    assert answer["error"]["code"] == "conflict"
    assert "changed since the preview" in answer["error"]["message"]
    malformed = _call(
        container, admin, "apply_domain", {**TARGET, "domain_id": domain_id, "plan_hash": "short"}
    )
    assert malformed["error"]["code"] == "invalid_input"
    assert api.world.helper.vhosts == {}


def test_check_certificate_and_remove(api: Api) -> None:
    container = _container(api)
    domain_id = api.create()
    api.apply(domain_id)
    admin = _principal(container, TokenScope.ADMIN)
    arguments = {**TARGET, "domain_id": domain_id}
    check = _call(container, admin, "check_domain", arguments)
    assert check["ok"] is True
    assert set(check["results"]) == {"dns", "https", "backend"}
    issued = _call(container, admin, "issue_certificate", arguments)
    assert issued["state"] == "ok"
    plan = _call(container, admin, "preview_domain_change", {**arguments, "operation": "remove"})
    assert plan["operation"] == "remove"
    removed = _call(
        container, admin, "remove_domain", {**arguments, "plan_hash": plan["plan_hash"]}
    )
    assert removed["operation"] == "remove"
    assert api.world.helper.vhosts == {}
    assert api.world.cloudflare.records == []


def test_a_domain_of_another_environment_is_not_found(api: Api) -> None:
    container = _container(api)
    domain_id = api.create()
    admin = _principal(container, TokenScope.ADMIN)
    answer = _call(
        container,
        admin,
        "check_domain",
        {"slug": "demo", "environment": "production", "domain_id": domain_id},
    )
    assert answer["error"]["code"] == "not_found"


# --- audit ------------------------------------------------------------------------------------


def test_domain_calls_are_audited_with_the_token_and_no_keys(api: Api) -> None:
    container = _container(api)
    domain_id = api.create()
    admin = _principal(container, TokenScope.ADMIN)
    plan = _call(container, admin, "preview_domain_change", {**TARGET, "domain_id": domain_id})
    _call(
        container,
        admin,
        "apply_domain",
        {**TARGET, "domain_id": domain_id, "plan_hash": plan["plan_hash"]},
    )
    events = AuditService(container.uow(), container.clock).list(limit=100)
    mcp = [e for e in events if e.action == "mcp.tool_call"]
    assert [(e.details["tool"], e.details["outcome"]) for e in reversed(mcp)] == [
        ("preview_domain_change", "ok"),
        ("apply_domain", "ok"),
    ]
    assert {(e.actor_type.value, e.actor_id) for e in mcp} == {("token", str(admin.token.id))}  # type: ignore[union-attr]
    apply_event = mcp[0]
    assert apply_event.details["arguments"]["plan_hash"] == plan["plan_hash"]  # type: ignore[index]
    # the services still audit the change itself, with the same token identity
    changed = [e for e in events if e.action == "domain.apply"]
    assert changed
    assert changed[0].actor_id == str(admin.token.id)  # type: ignore[union-attr]
    text = json.dumps([e.details for e in events]).lower()
    assert api.world.cloudflare.token.lower() not in text
    assert "private key" not in text
    assert "begin " not in text


# --- end to end with the SDK client -----------------------------------------------------------


def test_a_client_session_reads_domains_and_cannot_apply(api: Api) -> None:
    container = _container(api)
    domain_id = api.create()
    token = _token(container, TokenScope.READ)

    async def scenario() -> None:
        async with _session(container, token) as session:
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert names >= READ_TOOLS
            assert not {"apply_domain", "check_domain", "issue_certificate"} & names
            listed = await session.call_tool("list_domains", TARGET)
            assert not listed.is_error
            [item] = _text(listed)["items"]
            assert (item["id"], item["hostname"]) == (domain_id, HOST)
            denied = await session.call_tool(
                "apply_domain", {**TARGET, "domain_id": domain_id, "plan_hash": "a" * 64}
            )
            assert denied.is_error
            assert _text(denied)["error"]["code"] == "forbidden"

    _run(scenario())
    assert api.world.helper.vhosts == {}


@pytest.mark.parametrize("name", sorted(DOMAIN_TOOLS))
def test_every_domain_tool_has_a_strict_schema(name: str) -> None:
    tool = REGISTRY.get(name)
    assert tool is not None
    assert tool.input_schema["additionalProperties"] is False
