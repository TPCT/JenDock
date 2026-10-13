"""MCP server: registry, scopes, audit, and a real SDK client session against the ASGI app."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import Engine

from backend.adapters.worker_client import RepositoryFile
from backend.api.app import create_app
from backend.api.container import AppContainer
from backend.api.principal import Principal
from backend.domain.actor import Actor
from backend.domain.enums import EnvironmentName, PipelineSource, RunTrigger, TokenScope
from backend.mcp.dispatcher import ToolDispatcher, audit_arguments
from backend.mcp.registry import Tool, ToolRegistry
from backend.mcp.tools import default_registry, deploy
from backend.services.audit import AuditService
from backend.services.runs import RunService
from backend.services.secrets import SecretInfo
from backend.services.tokens import AuthenticatedToken
from tests.api_support import FakeClock, create_user, make_container, make_settings
from tests.conftest import DEV
from tests.github_support import SHA_A, SHA_B, FakeGithub

REGISTRY = default_registry()
BASE = "https://app.example.test"
VALID_PIPELINE = (
    "pipeline {\n  agent none\n  stages {\n    stage('A') {\n      steps { echo 'x' }\n"
    "    }\n  }\n}\n"
)


class FakeSecrets:
    """Names only, like the real gateway's ``list_secrets``."""

    def list_secrets(self, slug: str, env: EnvironmentName, actor: Actor) -> Sequence[SecretInfo]:
        now = datetime(2026, 10, 13, tzinfo=UTC)
        return [SecretInfo("DB_PASSWORD", now, now)]

    def reveal_secret(self, *_args: object) -> str:  # pragma: no cover - must never be called
        raise AssertionError("reveal must not be reachable from MCP")


class FakeRepository:
    """No Jenkinsfile in the repository."""

    def repository_jenkinsfile(
        self, slug: str, env: EnvironmentName, actor: Actor
    ) -> RepositoryFile:
        return RepositoryFile(True, None)


def _real_principal(container: AppContainer, *scopes: TokenScope) -> Principal:
    """A principal backed by a stored token (runs reference the token row)."""
    user = _user(container)
    created = container.token_service().create("agent", list(scopes), user, None, Actor.system("t"))
    authenticated = container.token_service().authenticate(created.value)
    assert authenticated is not None
    return Principal(token=authenticated)


def _user(container: AppContainer) -> int:
    with container.uow() as uow:
        existing = uow.users.get_by_username("admin")
        if existing is not None:
            return existing.id
    return create_user(container)


def _principal(*scopes: TokenScope, token_id: int = 7) -> Principal:
    return Principal(token=AuthenticatedToken(token_id, "agent", frozenset(scopes)))


@pytest.fixture
def github() -> FakeGithub:
    return FakeGithub({"development": SHA_A, "production": SHA_B})


@pytest.fixture
def wired(
    tmp_path: Path, engine: Engine, clock: FakeClock, github: FakeGithub, project: str
) -> AppContainer:
    container = make_container(make_settings(tmp_path), engine, clock, github=github)
    object.__setattr__(container, "secrets", FakeSecrets())
    object.__setattr__(container, "repository", FakeRepository())
    return container


@pytest.fixture
def dispatcher(wired: AppContainer) -> ToolDispatcher:
    return ToolDispatcher(REGISTRY, wired)


def _payload(result: object) -> dict[str, Any]:  # Any: decoded JSON of a tool result
    structured = getattr(result, "structured_content", None)
    assert isinstance(structured, dict)
    return structured


# --- registry --------------------------------------------------------------------------------


def test_the_registry_has_unique_well_formed_tools() -> None:
    tools = REGISTRY.all()
    names = [tool.name for tool in tools]
    assert len(names) == len(set(names))
    assert {
        "list_projects",
        "get_pipeline",
        "save_pipeline",
        "trigger_run",
        "read_step_log",
    } <= set(names)
    for tool in tools:
        assert tool.input_schema["type"] == "object"
        assert tool.input_schema["additionalProperties"] is False
        assert tool.description
        if tool.scope is TokenScope.READ:
            assert tool.read_only


def test_duplicate_names_are_refused() -> None:
    first = REGISTRY.get("list_projects")
    assert first is not None
    registry = ToolRegistry([first])
    with pytest.raises(ValueError, match="duplicate"):
        registry.add(first)


def test_there_is_no_tool_that_reveals_or_writes_secrets() -> None:
    for tool in REGISTRY.all():
        assert "reveal" not in tool.name
        assert not tool.name.startswith(("set_secret", "delete_secret"))


def test_deployment_tools_mirror_the_rest_scopes() -> None:
    scopes = {t.name: t.scope for t in deploy.TOOLS}
    assert scopes == {
        "list_deployments": TokenScope.READ,
        "preview_redeploy": TokenScope.DEPLOY,
        "redeploy": TokenScope.DEPLOY,
    }
    assert REGISTRY.get("rollback") is None  # the REST API has no rollback endpoint


def test_visibility_follows_the_scopes() -> None:
    reader = {t.name for t in REGISTRY.visible_to(_principal(TokenScope.READ))}
    assert {"list_runs", "get_pipeline", "preview_commit"} <= reader
    assert not {"trigger_run", "retry_run", "cancel_run", "save_pipeline"} & reader
    runner = {t.name for t in REGISTRY.visible_to(_principal(TokenScope.RUN))}
    assert runner == {"trigger_run", "retry_run", "cancel_run"}
    pipelines = {t.name for t in REGISTRY.visible_to(_principal(TokenScope.PIPELINES))}
    assert pipelines == {"save_pipeline"}
    admin = {t.name for t in REGISTRY.visible_to(_principal(TokenScope.ADMIN))}
    assert admin == {t.name for t in REGISTRY.all()}
    deployer = {t.name for t in REGISTRY.visible_to(_principal(TokenScope.DEPLOY))}
    assert deployer == {"preview_redeploy", "redeploy"}


# --- scope enforcement per tool --------------------------------------------------------------


@pytest.mark.parametrize("tool", REGISTRY.all(), ids=lambda t: t.name)
def test_every_tool_enforces_its_scope(dispatcher: ToolDispatcher, tool: Tool) -> None:
    others = [s for s in TokenScope if s not in (tool.scope, TokenScope.ADMIN)]
    for scope in others:
        denied = dispatcher.call(_principal(scope), tool.name, {})
        assert denied.is_error
        assert _payload(denied)["error"]["code"] == "forbidden"
    # with the right scope the call gets past authorisation (bad arguments, not "forbidden")
    allowed = dispatcher.call(_principal(tool.scope), tool.name, {"unexpected": 1})
    assert _payload(allowed)["error"]["code"] == "invalid_input"
    admin = dispatcher.call(_principal(TokenScope.ADMIN), tool.name, {"unexpected": 1})
    assert _payload(admin)["error"]["code"] == "invalid_input"


def test_unknown_tool_looks_like_a_missing_scope(dispatcher: ToolDispatcher) -> None:
    result = dispatcher.call(_principal(TokenScope.ADMIN), "format_disk", {})
    assert _payload(result)["error"]["code"] == "forbidden"


def test_invalid_input_never_reaches_the_handler(dispatcher: ToolDispatcher) -> None:
    admin = _principal(TokenScope.ADMIN)
    for arguments in (
        {"slug": "Not A Slug", "environment": "development"},
        {"slug": "demo", "environment": "staging"},
        {"slug": "demo"},
    ):
        result = dispatcher.call(admin, "get_pipeline", arguments)
        assert _payload(result)["error"]["code"] == "invalid_input"
    sha = dispatcher.call(
        admin,
        "trigger_run",
        {"slug": "demo", "environment": "development", "branch": "development", "commit_sha": "x"},
    )
    assert _payload(sha)["error"]["code"] == "invalid_input"


# --- tools use the platform's services -------------------------------------------------------


def test_read_tools_return_what_the_rest_api_returns(
    wired: AppContainer, dispatcher: ToolDispatcher
) -> None:
    reader = _principal(TokenScope.READ)
    projects = _payload(dispatcher.call(reader, "list_projects", {}))["items"]
    assert [p["slug"] for p in projects] == ["demo"]
    one = _payload(dispatcher.call(reader, "get_project", {"slug": "demo"}))
    assert one["repository"] == "octo/demo"
    missing = dispatcher.call(reader, "get_project", {"slug": "nope"})
    assert missing.is_error
    assert _payload(missing)["error"]["code"] == "not_found"
    catalog = _payload(dispatcher.call(reader, "get_pipeline_catalog", {}))
    assert any(step["name"] == "sh" for step in catalog["steps"])
    templates = _payload(dispatcher.call(reader, "get_pipeline_templates", {}))["items"]
    assert templates


def test_secret_names_are_listed_but_values_are_not_available(
    dispatcher: ToolDispatcher,
) -> None:
    result = dispatcher.call(
        _principal(TokenScope.READ),
        "list_secret_names",
        {"slug": "demo", "environment": "development"},
    )
    items = _payload(result)["items"]
    assert [i["name"] for i in items] == ["DB_PASSWORD"]
    assert set(items[0]) == {"name", "created_at", "updated_at"}


def test_validate_and_dry_run_and_save_a_pipeline(
    wired: AppContainer, dispatcher: ToolDispatcher
) -> None:
    target = {"slug": "demo", "environment": "development"}
    reader, writer = _principal(TokenScope.READ), _principal(TokenScope.PIPELINES)
    bad = _payload(dispatcher.call(reader, "validate_pipeline", {"jenkinsfile": "pipeline { x }"}))
    assert bad["diagnostics"]
    good = _payload(dispatcher.call(reader, "validate_pipeline", {"jenkinsfile": VALID_PIPELINE}))
    assert good["diagnostics"] == []
    dry = _payload(
        dispatcher.call(reader, "dry_run_pipeline", {**target, "jenkinsfile": VALID_PIPELINE})
    )
    assert dry["valid"] is True
    saved = _payload(
        dispatcher.call(
            writer,
            "save_pipeline",
            {**target, "jenkinsfile": VALID_PIPELINE, "base_version": 0},
        )
    )
    assert (saved.get("source"), saved.get("version")) == ("platform", 1), saved
    again = dispatcher.call(
        writer, "save_pipeline", {**target, "jenkinsfile": VALID_PIPELINE, "base_version": 0}
    )
    assert _payload(again)["error"]["code"] == "conflict"
    invalid = dispatcher.call(
        writer, "save_pipeline", {**target, "jenkinsfile": "pipeline { x }", "base_version": 1}
    )
    error = _payload(invalid)["error"]
    assert error["code"] == "validation_error"
    assert error["diagnostics"]
    fetched = _payload(dispatcher.call(reader, "get_pipeline", target))
    assert fetched["jenkinsfile"] == VALID_PIPELINE
    versions = _payload(dispatcher.call(reader, "list_pipeline_versions", target))["items"]
    assert [v["version"] for v in versions] == [1]


def test_manual_run_is_two_steps_and_rejects_a_moved_head(
    wired: AppContainer, dispatcher: ToolDispatcher, github: FakeGithub
) -> None:
    reader = _principal(TokenScope.READ)
    runner = _real_principal(wired, TokenScope.RUN)
    target = {"slug": "demo", "environment": "development", "branch": "development"}
    head = _payload(dispatcher.call(reader, "preview_commit", target))
    assert head["commit_sha"] == SHA_A
    assert head["message"]
    github.heads["development"] = SHA_B  # a push after the preview
    moved = dispatcher.call(runner, "trigger_run", {**target, "commit_sha": SHA_A})
    assert moved.is_error
    assert _payload(moved)["error"]["code"] == "conflict"
    github.heads["development"] = SHA_A
    run = _payload(dispatcher.call(runner, "trigger_run", {**target, "commit_sha": SHA_A}))
    assert (run["trigger"], run["status"], run["commit_sha"]) == ("manual", "queued", SHA_A)
    with wired.uow() as uow:
        row = uow.runs.get(int(run["id"]))
        assert row is not None
        assert runner.token is not None
        assert row.triggered_by_token_id == runner.token.id  # attributed to the token


def test_retry_cancel_and_list_runs(wired: AppContainer, dispatcher: ToolDispatcher) -> None:
    runner, reader = _real_principal(wired, TokenScope.RUN), _principal(TokenScope.READ)
    run = RunService(wired.uow(), wired.clock).create_queued(
        "demo",
        DEV,
        trigger=RunTrigger.PUSH,
        branch="development",
        commit_sha=SHA_A,
        pipeline_source=PipelineSource.PLATFORM,
        pipeline_text="",
    )
    early = dispatcher.call(runner, "retry_run", {"run_id": run.id})
    assert _payload(early)["error"]["code"] == "conflict"
    cancelled = _payload(dispatcher.call(runner, "cancel_run", {"run_id": run.id}))
    assert cancelled["outcome"] == "cancelled"
    retried = _payload(dispatcher.call(runner, "retry_run", {"run_id": run.id}))
    assert (retried["trigger"], retried["number"]) == ("manual", 2)
    listing = _payload(dispatcher.call(reader, "list_runs", {}))
    assert listing["total"] == 2
    scoped = _payload(
        dispatcher.call(reader, "list_runs", {"slug": "demo", "environment": "development"})
    )
    assert scoped["total"] == 2
    half = dispatcher.call(reader, "list_runs", {"slug": "demo"})
    assert _payload(half)["error"]["code"] == "validation_error"
    detail = _payload(dispatcher.call(reader, "get_run", {"run_id": run.id}))
    assert detail["status"] == "cancelled"
    log = dispatcher.call(reader, "read_step_log", {"run_id": run.id, "step_id": 999})
    assert _payload(log)["error"]["code"] == "not_found"


# --- audit -----------------------------------------------------------------------------------


def test_every_call_is_audited_with_the_token_identity(
    wired: AppContainer, dispatcher: ToolDispatcher
) -> None:
    dispatcher.call(_principal(TokenScope.READ, token_id=5), "get_project", {"slug": "demo"})
    dispatcher.call(_principal(TokenScope.READ, token_id=5), "trigger_run", {})  # denied
    dispatcher.call(_principal(TokenScope.READ, token_id=5), "get_project", {"slug": "nope"})
    dispatcher.call(_principal(TokenScope.READ, token_id=5), "get_project", {"bad": 1})
    events = AuditService(wired.uow(), wired.clock).list(action="mcp.tool_call")
    assert [(e.details["tool"], e.details["outcome"]) for e in reversed(list(events))] == [
        ("get_project", "ok"),
        ("trigger_run", "denied"),
        ("get_project", "error"),
        ("get_project", "invalid"),
    ]
    assert {(e.actor_type.value, e.actor_id) for e in events} == {("token", "5")}
    ok = events[-1]
    assert ok.details["arguments"] == {"slug": "demo"}
    assert ok.target_type == "mcp_tool"


def test_large_texts_are_not_stored_in_the_audit_log(
    wired: AppContainer, dispatcher: ToolDispatcher
) -> None:
    dispatcher.call(
        _principal(TokenScope.PIPELINES),
        "save_pipeline",
        {
            "slug": "demo",
            "environment": "development",
            "jenkinsfile": VALID_PIPELINE,
            "base_version": 0,
        },
    )
    [event] = AuditService(wired.uow(), wired.clock).list(action="mcp.tool_call")
    arguments = event.details["arguments"]
    assert isinstance(arguments, dict)
    assert arguments["jenkinsfile"] == f"<{len(VALID_PIPELINE)} characters>"
    assert "stage" not in json.dumps(event.details)


def test_audit_arguments_clip_and_omit() -> None:
    tool = REGISTRY.get("save_pipeline")
    recorded = audit_arguments(
        tool, {"jenkinsfile": "x" * 5000, "slug": "s" * 1000, "n": 3, "nested": {"a": 1}}
    )
    assert recorded["jenkinsfile"] == "<5000 characters>"
    assert recorded["slug"] == "s" * 200
    assert recorded["n"] == 3
    assert recorded["nested"] == "<omitted>"


# --- end to end with the SDK client ----------------------------------------------------------


@asynccontextmanager
async def _session(
    container: AppContainer, token: str | None, headers: dict[str, str] | None = None
) -> AsyncIterator[ClientSession]:
    app = create_app(container=container)
    sent = dict(headers or {})
    if token is not None:
        sent["Authorization"] = f"Bearer {token}"
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        client = httpx2.AsyncClient(transport=transport, base_url=BASE, headers=sent)
        async with (
            client as http,
            streamable_http_client(f"{BASE}/mcp", http_client=http) as streams,
            ClientSession(streams[0], streams[1]) as session,
        ):
            await session.initialize()
            yield session


def _token(container: AppContainer, *scopes: TokenScope) -> str:
    user = _user(container)
    created = container.token_service().create(
        "agent", list(scopes), user, None, Actor.system("test")
    )
    return created.value


def _run[T](coroutine: Awaitable[T]) -> T:
    async def runner() -> T:
        return await coroutine

    return asyncio.run(runner())


def _text(result: object) -> dict[str, Any]:  # Any: decoded JSON of a tool result
    content = getattr(result, "content", [])
    data = json.loads(content[0].text)
    assert isinstance(data, dict)
    return data


def test_a_client_session_lists_tools_and_calls_read_and_write_tools(wired: AppContainer) -> None:
    token = _token(wired, TokenScope.READ)

    async def scenario() -> None:
        async with _session(wired, token) as session:
            listed = await session.list_tools()
            names = {tool.name for tool in listed.tools}
            assert "list_projects" in names
            assert "trigger_run" not in names  # not in the token's scopes
            read = await session.call_tool("list_projects", {})
            assert not read.is_error
            assert _text(read)["items"][0]["slug"] == "demo"
            denied = await session.call_tool(
                "trigger_run",
                {
                    "slug": "demo",
                    "environment": "development",
                    "branch": "development",
                    "commit_sha": SHA_A,
                },
            )
            assert denied.is_error
            assert _text(denied)["error"]["code"] == "forbidden"

    _run(scenario())
    events = AuditService(wired.uow(), wired.clock).list(action="mcp.tool_call")
    assert [(e.details["tool"], e.details["outcome"]) for e in reversed(list(events))] == [
        ("list_projects", "ok"),
        ("trigger_run", "denied"),
    ]
    assert {e.actor_type.value for e in events} == {"token"}


def test_a_token_with_the_run_scope_can_start_a_run_over_mcp(wired: AppContainer) -> None:
    token = _token(wired, TokenScope.READ, TokenScope.RUN)

    async def scenario() -> None:
        async with _session(wired, token) as session:
            target = {"slug": "demo", "environment": "development", "branch": "development"}
            head = _text(await session.call_tool("preview_commit", target))
            run = await session.call_tool(
                "trigger_run", {**target, "commit_sha": head["commit_sha"]}
            )
            assert not run.is_error
            assert _text(run)["status"] == "queued"

    _run(scenario())


def test_the_endpoint_needs_a_valid_bearer_token(wired: AppContainer) -> None:
    create_user(wired)

    async def post(headers: dict[str, str]) -> httpx2.Response:
        app = create_app(container=wired)
        async with app.router.lifespan_context(app):
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(transport=transport, base_url=BASE) as http:
                body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
                return await http.post(
                    "/mcp",
                    json=body,
                    headers={"Accept": "application/json, text/event-stream", **headers},
                )

    for headers in (
        {},
        {"Authorization": "Bearer jdt_nope"},
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer "},
    ):
        response = _run(post(headers))
        assert response.status_code == 401, headers
        assert response.headers["www-authenticate"].startswith("Bearer")


def test_session_cookies_are_never_accepted(wired: AppContainer) -> None:
    async def scenario() -> int:
        app = create_app(container=wired)
        user = create_user(wired)
        assert user
        async with app.router.lifespan_context(app):
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(transport=transport, base_url=BASE) as http:
                login = await http.post(
                    "/api/auth/login",
                    json={"username": "admin", "password": "correct horse battery"},
                    headers={"Origin": BASE},
                )
                assert login.status_code == 200
                body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
                response = await http.post(
                    "/mcp",
                    json=body,
                    headers={"Accept": "application/json, text/event-stream"},
                )
                return response.status_code

    assert _run(scenario()) == 401


def test_a_foreign_origin_is_refused_even_with_a_valid_token(wired: AppContainer) -> None:
    token = _token(wired, TokenScope.ADMIN)

    async def post(origin: str | None) -> int:
        app = create_app(container=wired)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        }
        if origin is not None:
            headers["Origin"] = origin
        async with app.router.lifespan_context(app):
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(transport=transport, base_url=BASE) as http:
                body = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}}
                return (await http.post("/mcp", json=body, headers=headers)).status_code

    assert _run(post("https://evil.example")) == 403
    assert _run(post(BASE)) != 403
    assert _run(post(None)) != 403


def test_the_endpoint_can_be_switched_off(tmp_path: Path, engine: Engine, clock: FakeClock) -> None:
    settings = make_settings(tmp_path).model_copy(update={"mcp_enabled": False})
    container = make_container(settings, engine, clock)

    async def status() -> int:
        app = create_app(container=container)
        async with app.router.lifespan_context(app):
            transport = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(transport=transport, base_url=BASE) as http:
                return (await http.post("/mcp", json={})).status_code

    assert _run(status()) in (404, 405)
