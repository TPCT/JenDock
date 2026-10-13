"""MCP deployment tools: history, redeploy preview and the guarded redeploy."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine

from backend.adapters.deploy_ledger import ServiceChange
from backend.api.container import AppContainer
from backend.api.principal import Principal
from backend.domain.enums import (
    DeploymentStatus,
    EnvironmentName,
    PipelineSource,
    RunTrigger,
    TokenScope,
)
from backend.mcp.dispatcher import ToolDispatcher
from backend.mcp.tools import default_registry
from backend.repositories.models import Deployment
from backend.services.audit import AuditService
from backend.services.runs import RunService
from tests.api_support import FakeClock, make_container, make_settings
from tests.conftest import DEV, PROD
from tests.github_support import SHA_A
from tests.test_mcp import _real_principal, _run, _session, _text, _token

REGISTRY = default_registry()
TARGET = {"slug": "demo", "environment": "development"}
OLD, NEW = "app-web:old", "sc/demo-development-web:0123456789ab-r1"


@pytest.fixture
def wired(tmp_path: Path, engine: Engine, clock: FakeClock, project: str) -> AppContainer:
    return make_container(make_settings(tmp_path), engine, clock)


def _payload(result: object) -> dict[str, Any]:  # Any: decoded JSON of a tool result
    structured = getattr(result, "structured_content", None)
    assert isinstance(structured, dict)
    return structured


def _call(
    container: AppContainer, principal: Principal, name: str, arguments: Mapping[str, object]
) -> dict[str, Any]:
    return _payload(ToolDispatcher(REGISTRY, container).call(principal, name, arguments))


def _deployment(
    container: AppContainer,
    status: DeploymentStatus = DeploymentStatus.SUCCEEDED,
    env: EnvironmentName = DEV,
    previous: str | None = OLD,
    image: str = NEW,
) -> int:
    """A finished run and its deployment row (what the deployer's ledger writes)."""
    container.clock()  # the fake clock keeps rows ordered through ``created_at``
    run = RunService(container.uow(), container.clock).create_queued(
        "demo",
        env,
        trigger=RunTrigger.PUSH,
        branch=env.value,
        commit_sha=SHA_A,
        pipeline_source=PipelineSource.PLATFORM,
        pipeline_text="",
    )
    change = ServiceChange("web", image, "sha256:new", previous, "sha256:old")
    with container.uow() as uow:
        row = uow.deployments.add(
            Deployment(
                run_id=run.id,
                environment_id=run.environment_id,
                status=status,
                commit_sha=SHA_A,
                services=[change.to_json()],
                created_at=container.clock(),
            )
        )
        deployment_id = row.id
    container.clock.advance(seconds=1)  # type: ignore[attr-defined]
    return deployment_id


# --- registry and scopes ---------------------------------------------------------------------


def test_scopes_filter_the_deployment_tools(wired: AppContainer) -> None:
    def visible(*scopes: TokenScope) -> set[str]:
        principal = Principal(token=_authenticated(wired, *scopes))
        return {t.name for t in REGISTRY.visible_to(principal)} & {
            "list_deployments",
            "preview_redeploy",
            "redeploy",
        }

    assert visible(TokenScope.READ) == {"list_deployments"}
    assert visible(TokenScope.DEPLOY) == {"preview_redeploy", "redeploy"}
    assert visible(TokenScope.RUN) == set()
    assert visible(TokenScope.ADMIN) == {"list_deployments", "preview_redeploy", "redeploy"}


def _authenticated(container: AppContainer, *scopes: TokenScope):  # type: ignore[no-untyped-def]
    principal = _real_principal(container, *scopes)
    assert principal.token is not None
    return principal.token


def test_a_read_or_run_token_cannot_redeploy(wired: AppContainer) -> None:
    deployment_id = _deployment(wired)
    arguments = {**TARGET, "deployment_id": deployment_id}
    for scope in (TokenScope.READ, TokenScope.RUN, TokenScope.PIPELINES):
        principal = _real_principal(wired, scope)
        for name in ("preview_redeploy", "redeploy"):
            assert _call(wired, principal, name, arguments)["error"]["code"] == "forbidden"
    with wired.uow() as uow:
        assert uow.runs.count() == 1


# --- history ---------------------------------------------------------------------------------


def test_list_deployments_matches_the_history_shape(wired: AppContainer) -> None:
    first = _deployment(wired)
    second = _deployment(wired, DeploymentStatus.ROLLED_BACK, previous=NEW, image="web:bad")
    reader = _real_principal(wired, TokenScope.READ)
    page = _call(wired, reader, "list_deployments", {**TARGET, "limit": 1})
    assert (page["total"], page["limit"], page["offset"]) == (2, 1, 0)
    assert [d["id"] for d in page["items"]] == [second]
    later = _call(wired, reader, "list_deployments", {**TARGET, "limit": 5, "offset": 1})
    assert [d["id"] for d in later["items"]] == [first]
    [service] = later["items"][0]["services"]
    assert (service["service"], service["previous_image"], service["new_image"]) == (
        "web",
        OLD,
        NEW,
    )
    assert (
        _call(wired, reader, "list_deployments", {"slug": "demo", "environment": "production"})[
            "total"
        ]
        == 0
    )
    bad = _call(wired, reader, "list_deployments", {**TARGET, "limit": 101})
    assert bad["error"]["code"] == "invalid_input"
    missing = _call(
        wired, reader, "list_deployments", {"slug": "nope", "environment": "development"}
    )
    assert missing["error"]["code"] == "not_found"


# --- preview and redeploy --------------------------------------------------------------------


def test_preview_shows_what_would_deploy_and_what_runs_now(wired: AppContainer) -> None:
    old = _deployment(wired, image="web:v1", previous=None)
    _deployment(wired, image="web:v2", previous="web:v1")
    deployer = _real_principal(wired, TokenScope.DEPLOY)
    preview = _call(wired, deployer, "preview_redeploy", {**TARGET, "deployment_id": old})
    assert preview["would_deploy"] == [{"service": "web", "image": "web:v1"}]
    assert [(r["service"], r["image"]) for r in preview["running_now"]] == [("web", "web:v2")]
    assert preview["in_progress"] is False
    assert preview["can_redeploy"] is True
    with wired.uow() as uow:  # a preview queues nothing
        assert uow.runs.count() == 2


def test_running_images_follow_rollbacks(wired: AppContainer) -> None:
    good = _deployment(wired, image="web:v1", previous=None)
    _deployment(wired, DeploymentStatus.ROLLED_BACK, image="web:v2", previous="web:v1")
    deployer = _real_principal(wired, TokenScope.DEPLOY)
    preview = _call(wired, deployer, "preview_redeploy", {**TARGET, "deployment_id": good})
    assert [r["image"] for r in preview["running_now"]] == ["web:v1"]  # v2 was rolled back


def test_redeploy_queues_a_redeploy_run_attributed_to_the_token(wired: AppContainer) -> None:
    deployment_id = _deployment(wired)
    deployer = _real_principal(wired, TokenScope.DEPLOY)
    result = _call(wired, deployer, "redeploy", {**TARGET, "deployment_id": deployment_id})
    run = result["run"]
    assert (run["trigger"], run["status"], run["commit_sha"]) == ("redeploy", "queued", SHA_A)
    assert result["deployment_id"] != deployment_id  # a pending row of its own
    with wired.uow() as uow:
        row = uow.runs.get(run["id"])
        assert row is not None
        assert deployer.token is not None
        assert row.triggered_by_token_id == deployer.token.id
    page = _call(wired, _real_principal(wired, TokenScope.READ), "list_deployments", TARGET)
    assert page["items"][0]["status"] == "pending"
    assert page["items"][0]["redeploy_of"] == deployment_id


def test_redeploy_is_refused_while_a_deployment_is_in_progress(wired: AppContainer) -> None:
    done = _deployment(wired)
    deployer = _real_principal(wired, TokenScope.DEPLOY)
    arguments = {**TARGET, "deployment_id": done}
    first = _call(wired, deployer, "redeploy", arguments)
    assert "run" in first
    preview = _call(wired, deployer, "preview_redeploy", arguments)
    assert preview["in_progress"] is True  # the pending redeploy
    assert preview["can_redeploy"] is False
    second = _call(wired, deployer, "redeploy", arguments)
    assert second["error"]["code"] == "conflict"
    assert "in progress" in second["error"]["message"]
    with wired.uow() as uow:
        assert uow.runs.count() == 2  # the original run and the first redeploy only


def test_a_running_deployment_blocks_too(wired: AppContainer) -> None:
    done = _deployment(wired)
    _deployment(wired, DeploymentStatus.DEPLOYING, image="web:next", previous=NEW)
    deployer = _real_principal(wired, TokenScope.DEPLOY)
    answer = _call(wired, deployer, "redeploy", {**TARGET, "deployment_id": done})
    assert answer["error"]["code"] == "conflict"


@pytest.mark.parametrize(
    "status",
    [DeploymentStatus.FAILED, DeploymentStatus.ROLLED_BACK, DeploymentStatus.DEPLOYING],
)
def test_only_a_succeeded_deployment_can_be_redeployed(
    wired: AppContainer, status: DeploymentStatus
) -> None:
    deployment_id = _deployment(wired, status)
    deployer = _real_principal(wired, TokenScope.DEPLOY)
    for name in ("preview_redeploy", "redeploy"):
        answer = _call(wired, deployer, name, {**TARGET, "deployment_id": deployment_id})
        assert answer["error"]["code"] == "conflict", name


def test_a_deployment_of_another_environment_is_not_found(wired: AppContainer) -> None:
    other = _deployment(wired, env=PROD)
    deployer = _real_principal(wired, TokenScope.DEPLOY)
    for name in ("preview_redeploy", "redeploy"):
        answer = _call(wired, deployer, name, {**TARGET, "deployment_id": other})
        assert answer["error"]["code"] == "not_found", name
    unknown = _call(wired, deployer, "redeploy", {**TARGET, "deployment_id": 9999})
    assert unknown["error"]["code"] == "not_found"


# --- audit -----------------------------------------------------------------------------------


def test_calls_are_audited_with_the_token(wired: AppContainer) -> None:
    deployment_id = _deployment(wired)
    deployer = _real_principal(wired, TokenScope.DEPLOY)
    _call(wired, deployer, "preview_redeploy", {**TARGET, "deployment_id": deployment_id})
    _call(wired, deployer, "redeploy", {**TARGET, "deployment_id": deployment_id})
    events = AuditService(wired.uow(), wired.clock).list(limit=50)
    mcp = [e for e in events if e.action == "mcp.tool_call"]
    assert [(e.details["tool"], e.details["outcome"]) for e in reversed(mcp)] == [
        ("preview_redeploy", "ok"),
        ("redeploy", "ok"),
    ]
    assert deployer.token is not None
    assert {(e.actor_type.value, e.actor_id) for e in mcp} == {("token", str(deployer.token.id))}
    assert mcp[0].details["arguments"] == {**TARGET, "deployment_id": deployment_id}
    service_event = next(e for e in events if e.action == "deployment.redeploy")
    assert service_event.actor_id == str(deployer.token.id)


# --- end to end with the SDK client ----------------------------------------------------------


def test_a_client_session_lists_and_redeploys_with_the_right_scopes(wired: AppContainer) -> None:
    deployment_id = _deployment(wired)
    reader = _token(wired, TokenScope.READ)
    deployer = _token(wired, TokenScope.READ, TokenScope.DEPLOY)

    async def as_reader() -> None:
        async with _session(wired, reader) as session:
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert "list_deployments" in names
            assert not {"redeploy", "preview_redeploy"} & names
            listed = await session.call_tool("list_deployments", TARGET)
            assert [d["id"] for d in _text(listed)["items"]] == [deployment_id]
            denied = await session.call_tool("redeploy", {**TARGET, "deployment_id": deployment_id})
            assert denied.is_error
            assert _text(denied)["error"]["code"] == "forbidden"

    async def as_deployer() -> None:
        async with _session(wired, deployer) as session:
            arguments = {**TARGET, "deployment_id": deployment_id}
            preview = _text(await session.call_tool("preview_redeploy", arguments))
            assert preview["can_redeploy"] is True
            done = await session.call_tool("redeploy", arguments)
            assert not done.is_error
            assert _text(done)["run"]["trigger"] == "redeploy"

    _run(as_reader())
    _run(as_deployer())
