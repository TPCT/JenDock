"""MCP tools use the same policy as REST: grants, listing filters and token scopes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from backend.adapters.worker_client import WorkerClient
from backend.api.principal import Principal
from backend.domain.access import RULES, Action
from backend.domain.actor import Actor
from backend.domain.database import DatabaseEngine, password_secret_name
from backend.domain.enums import EnvironmentName, TokenScope
from backend.mcp.access import TOOL_ACCESS
from backend.mcp.dispatcher import ToolDispatcher
from backend.mcp.tools import default_registry
from backend.repositories.models import DatabaseConnection
from backend.services.audit import AuditService
from backend.services.database_backups import DatabaseBackupService
from tests.access_support import SYSTEM, WEB, WORKER
from tests.access_world import World
from tests.github_support import SHA_A

REGISTRY = default_registry()
DEV = {"slug": "demo", "environment": "development"}
PROD = {"slug": "demo", "environment": "production"}


def _principal(world: World, who: str, *scopes: TokenScope) -> Principal:
    """A token principal of the user ``who`` (scopes default to all of them)."""
    user_id = world[who].user_id
    chosen = list(scopes) or list(TokenScope)
    created = world.container.token_service().create("agent", chosen, user_id, None, SYSTEM)
    token = world.container.token_service().authenticate(created.value)
    assert token is not None
    grants = world.container.access_service().load(user_id)
    return Principal(token=token, grants=grants)


def _owner_principal(world: World, *scopes: TokenScope) -> Principal:
    created = world.container.token_service().create(
        "agent", list(scopes) or list(TokenScope), world.owner.user_id, None, SYSTEM
    )
    token = world.container.token_service().authenticate(created.value)
    assert token is not None
    return Principal(token=token)


def _call(
    world: World, principal: Principal, name: str, args: Mapping[str, object]
) -> dict[str, Any]:
    result = ToolDispatcher(REGISTRY, world.container).call(principal, name, args)
    structured = result.structured_content
    assert isinstance(structured, dict)
    return {**structured, "_error": result.is_error}


def _code(answer: dict[str, Any]) -> str | None:
    return answer["error"]["code"] if answer["_error"] else None


# --- the rule table covers every tool ---------------------------------------------------------


def test_every_tool_is_mapped_to_an_action_and_nothing_else_is() -> None:
    assert {tool.name for tool in REGISTRY.all()} == set(TOOL_ACCESS)


@pytest.mark.parametrize("tool", REGISTRY.all(), ids=lambda tool: tool.name)
def test_tool_scope_equals_the_scope_of_its_action(tool: Any) -> None:
    access = TOOL_ACCESS[tool.name]
    assert RULES[access.action].scope is tool.scope


# --- lists are filtered -----------------------------------------------------------------------


def test_list_projects_and_runs_only_show_granted_things(world: World) -> None:
    viewer = _principal(world, "viewer")
    projects = _call(world, viewer, "list_projects", {})
    assert [p["slug"] for p in projects["items"]] == ["demo"]
    assert _call(world, _principal(world, "nobody"), "list_projects", {})["items"] == []
    runs = _call(world, viewer, "list_runs", {})
    assert {r["project"] for r in runs["items"]} == {"demo"} and runs["total"] == 2
    devonly = _call(world, _principal(world, "devonly"), "list_projects", {})
    assert [e["name"] for e in devonly["items"][0]["environments"]] == ["development"]
    owner = _call(world, _owner_principal(world), "list_runs", {})
    assert owner["total"] == 3


def test_reads_of_hidden_things_are_not_found(world: World) -> None:
    viewer = _principal(world, "viewer")
    assert _code(_call(world, viewer, "get_project", {"slug": "other"})) == "not_found"
    assert _call(world, viewer, "get_project", {"slug": "demo"})["slug"] == "demo"
    other_run = world.runs["other_dev"]
    assert _code(_call(world, viewer, "get_run", {"run_id": other_run})) == "not_found"
    assert (
        _code(
            _call(world, viewer, "list_runs", {**{"slug": "other"}, "environment": "development"})
        )
        == "not_found"
    )
    devonly = _principal(world, "devonly")
    assert _code(_call(world, devonly, "list_runs", PROD)) == "not_found"
    assert (
        _code(_call(world, devonly, "get_run", {"run_id": world.runs["demo_prod"]})) == "not_found"
    )


# --- actions need the role --------------------------------------------------------------------


def test_trigger_retry_and_cancel_follow_the_roles(world: World) -> None:
    trigger = {**DEV, "branch": "development", "commit_sha": SHA_A}
    assert _code(_call(world, _principal(world, "viewer"), "trigger_run", trigger)) == "forbidden"
    assert _code(_call(world, _principal(world, "developer"), "trigger_run", trigger)) is None
    prod = {**PROD, "branch": "production", "commit_sha": "b" * 40}
    assert _code(_call(world, _principal(world, "developer"), "trigger_run", prod)) == "forbidden"
    assert _code(_call(world, _principal(world, "maintainer"), "trigger_run", prod)) is None
    dev_run = {"run_id": world.runs["demo_dev"]}
    assert _code(_call(world, _principal(world, "viewer"), "cancel_run", dev_run)) == "forbidden"
    assert _code(_call(world, _principal(world, "developer"), "cancel_run", dev_run)) is None
    assert _code(_call(world, _principal(world, "nobody"), "retry_run", dev_run)) == "not_found"


def test_pipeline_deploy_domain_and_database_tools(world: World) -> None:
    viewer, developer = _principal(world, "viewer"), _principal(world, "developer")
    maintainer = _principal(world, "maintainer")
    save = {**DEV, "jenkinsfile": "pipeline {}", "base_version": 0}
    assert _code(_call(world, viewer, "save_pipeline", save)) == "forbidden"
    assert (
        _code(
            _call(world, developer, "save_pipeline", {**PROD, **save, "environment": "production"})
        )
        == "forbidden"
    )
    assert _code(_call(world, viewer, "redeploy", {**DEV, "deployment_id": 1})) == "forbidden"
    assert (
        _code(_call(world, developer, "preview_redeploy", {**DEV, "deployment_id": 1}))
        == "forbidden"
    )
    assert _code(_call(world, viewer, "list_deployments", PROD)) is None
    assert _code(_call(world, viewer, "list_certificates", {})) == "forbidden"
    assert _code(_call(world, maintainer, "list_certificates", {})) == "forbidden"
    domain = {**DEV, "domain_id": 1}
    assert _code(_call(world, developer, "check_domain", domain)) == "forbidden"
    assert _code(_call(world, viewer, "list_domains", DEV)) is None
    rows = {**PROD, "connection": "c", "schema": "s", "table": "t"}
    assert _code(_call(world, developer, "read_table_rows", rows)) == "forbidden"
    assert _code(_call(world, viewer, "list_database_connections", PROD)) is None
    assert _code(_call(world, _principal(world, "nobody"), "list_secret_names", DEV)) == "not_found"
    assert _code(_call(world, viewer, "list_secret_names", DEV)) not in {"forbidden", "not_found"}


def test_validating_against_an_environment_needs_to_see_it(world: World) -> None:
    text = {"jenkinsfile": "pipeline {}"}
    nobody = _principal(world, "nobody")
    assert _code(_call(world, nobody, "validate_pipeline", text)) is None
    assert _code(_call(world, nobody, "validate_pipeline", {**text, **DEV})) == "not_found"
    assert (
        _code(_call(world, _principal(world, "viewer"), "validate_pipeline", {**text, **DEV}))
        is None
    )
    assert _code(_call(world, nobody, "get_pipeline_catalog", {})) is None


# --- containers: roles and per-service grants -------------------------------------------------


def test_container_tools_with_roles_and_service_grants(world: World) -> None:
    viewer, maintainer = _principal(world, "viewer"), _principal(world, "maintainer")
    listed = _call(world, viewer, "list_containers", DEV)["items"]
    assert [c["service"] for c in listed] == ["web", "worker"]
    target = {**DEV, "container_id": WEB}
    assert _code(_call(world, viewer, "tail_container_logs", target)) == "forbidden"
    assert _code(_call(world, maintainer, "tail_container_logs", target)) is None
    logs, actions = _principal(world, "logs"), _principal(world, "actions")
    assert [c["service"] for c in _call(world, logs, "list_containers", DEV)["items"]] == ["web"]
    assert _code(_call(world, logs, "tail_container_logs", target)) is None
    assert (
        _code(_call(world, logs, "tail_container_logs", {**DEV, "container_id": WORKER}))
        == "forbidden"
    )
    assert _code(_call(world, actions, "tail_container_logs", target)) == "forbidden"
    plan = {**target, "action": "restart"}
    assert _code(_call(world, actions, "preview_container_action", plan)) is None
    assert _code(_call(world, logs, "preview_container_action", plan)) == "forbidden"
    limits = {**target, "cpus": 1}
    assert _code(_call(world, logs, "update_container_limits", limits)) == "forbidden"
    assert _code(_call(world, _principal(world, "nobody"), "list_containers", DEV)) == "not_found"
    assert _code(_call(world, _principal(world, "nobody"), "host_telemetry", {})) == "forbidden"


# --- tokens: scopes and users -----------------------------------------------------------------


def test_scopes_and_roles_both_have_to_allow(world: World) -> None:
    trigger = {**DEV, "branch": "development", "commit_sha": SHA_A}
    read_only = _principal(world, "developer", TokenScope.READ)
    assert _code(_call(world, read_only, "trigger_run", trigger)) == "forbidden"
    everything = _principal(world, "viewer")  # every scope, but a viewer
    assert _code(_call(world, everything, "trigger_run", trigger)) == "forbidden"
    assert _code(_call(world, everything, "redeploy", {**DEV, "deployment_id": 1})) == "forbidden"
    assert _code(_call(world, everything, "list_runs", {})) is None


def test_tool_listing_hides_what_the_user_could_never_do(world: World) -> None:
    def names(principal: Principal) -> set[str]:
        return {tool.name for tool in REGISTRY.visible_to(principal)}

    nobody = names(_principal(world, "nobody"))
    assert "get_pipeline_catalog" in nobody and "list_projects" in nobody
    assert "list_certificates" not in nobody and "trigger_run" not in nobody
    assert "list_certificates" not in names(_principal(world, "maintainer"))
    assert "list_certificates" in names(_owner_principal(world))
    read_only = names(_principal(world, "maintainer", TokenScope.READ))
    assert "trigger_run" not in read_only and "get_run" in read_only
    assert Action.RUN_CREATE in {a.action for a in TOOL_ACCESS.values()}


def test_denials_are_audited_with_the_token(world: World) -> None:
    principal = _principal(world, "viewer")
    _call(world, principal, "trigger_run", {**DEV, "branch": "development", "commit_sha": SHA_A})
    event = AuditService(world.container.uow()).list(action="mcp.tool_call")[0]
    assert event.details["outcome"] == "denied" and event.details["tool"] == "trigger_run"
    assert principal.token is not None and event.actor_id == str(principal.token.id)


def test_backup_tools_use_backup_read_and_run_actions(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    with world.container.uow() as uow:
        environment = uow.environments.get_by_project_slug("demo", EnvironmentName.DEVELOPMENT)
        assert environment is not None
        connection = uow.database_connections.add(
            DatabaseConnection(
                environment_id=environment.id,
                name="main",
                engine=DatabaseEngine.POSTGRESQL,
                host="db",
                port=5432,
                database="app",
                username="fixture_reader",
                password_secret=password_secret_name("main"),
            )
        )
        connection_id, environment_id = connection.id, environment.id
    backup_service = DatabaseBackupService(world.container.uow(), world.container.clock)
    record = backup_service.create(connection_id, environment_id, "manual")
    backup_service.finish(
        record.id, "succeeded", size_bytes=10, object_key="backups/demo/dev/main.dump"
    )

    viewer = _principal(world, "viewer")
    listed = _call(
        world,
        viewer,
        "list_backups",
        {"slug": "demo", "environment": "development"},
    )
    assert len(listed["items"]) == 1 and listed["items"][0]["connection"] == "main"
    assert (
        _code(
            _call(
                world,
                _principal(world, "nobody"),
                "list_backups",
                {"slug": "demo", "environment": "development"},
            )
        )
        == "not_found"
    )

    for username in ("viewer", "developer"):
        denied = _call(
            world,
            _principal(world, username),
            "run_backup",
            {"slug": "demo", "environment": "development", "connection": "main"},
        )
        assert _code(denied) == "forbidden"

    calls: list[str] = []

    def fake_call(
        _self: WorkerClient,
        method: str,
        _actor: Actor | None,
        _params: Mapping[str, object],
        *,
        timeout: float | None = None,
    ) -> object:
        calls.append(method)
        return {"started": True}

    monkeypatch.setattr(WorkerClient, "call", fake_call)
    ran = _call(
        world,
        _principal(world, "maintainer"),
        "run_backup",
        {"slug": "demo", "environment": "development", "connection": "main"},
    )
    assert not ran["_error"] and ran["status"] == "running"
    assert calls == ["database_backups.run"]


def test_a_disabled_owner_voids_the_token(world: World) -> None:
    principal = _principal(world, "viewer")
    assert principal.token is not None
    world.owner.patch(f"/api/users/{world['viewer'].user_id}", {"is_active": False})
    assert world.container.token_service().current(principal.token.id) is None
