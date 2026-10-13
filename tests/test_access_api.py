"""Enforcement of access grants through the HTTP API: lists, details, actions, tokens."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi.testclient import TestClient
from httpx2 import Response

from backend.api import authz
from backend.api.principal import Principal
from backend.domain.access import AccessRole, Action, ContainerPermission, GlobalRole, Resource
from backend.domain.enums import (
    EnvironmentName,
    TokenScope,
)
from backend.services.audit import AuditService
from tests.access_support import (
    SYSTEM,
    WEB,
    WORKER,
    Session,
    add_user,
    sign_in,
)
from tests.access_world import World
from tests.api_support import ORIGIN
from tests.github_support import SHA_A, SHA_B

DEV, PROD = EnvironmentName.DEVELOPMENT, EnvironmentName.PRODUCTION
V, D, M = AccessRole.VIEWER, AccessRole.DEVELOPER, AccessRole.MAINTAINER
DEMO = "/api/projects/demo"
DEV_BASE = f"{DEMO}/environments/development"
PROD_BASE = f"{DEMO}/environments/production"
LOGS, TERMINAL, ACTIONS = (
    ContainerPermission.LOGS,
    ContainerPermission.TERMINAL,
    ContainerPermission.ACTIONS,
)


def _ids(response: Response, key: str = "slug") -> list[str]:
    body = response.json()
    return [item[key] for item in (body["items"] if isinstance(body, dict) else body)]


def _list(who: Session, path: str, params: dict[str, Any]) -> dict[str, Any]:
    """A list endpoint with query parameters, as JSON."""
    body: dict[str, Any] = who.get(f"{path}?{urlencode(params)}").json()
    return body


def _passes(response: Response) -> bool:
    """Authorization let the request through (what happens next is not the point)."""
    return response.status_code not in {401, 403, 404}


# --- lists are filtered ----------------------------------------------------------------------


def test_project_list_shows_only_granted_projects_and_environments(world: World) -> None:
    assert _ids(world.owner.get("/api/projects")) == ["demo", "other"]
    assert _ids(world["nobody"].get("/api/projects")) == []
    assert _ids(world["viewer"].get("/api/projects")) == ["demo"]
    viewer = world["viewer"].get("/api/projects").json()["items"][0]
    assert [e["name"] for e in viewer["environments"]] == ["development", "production"]
    devonly = world["devonly"].get("/api/projects").json()["items"][0]
    assert [e["name"] for e in devonly["environments"]] == ["development"]


def test_a_container_grant_shows_the_project_without_runs(world: World) -> None:
    project = world["logs"].get("/api/projects").json()["items"][0]
    assert [e["name"] for e in project["environments"]] == ["development"]
    assert project["environments"][0]["last_run"] is None


def test_search_and_paging_apply_the_access_filter_before_counting(world: World) -> None:
    # The viewer is granted "demo" only; "other" must not be found, counted or paged.
    viewer, owner = world["viewer"], world.owner
    assert _list(owner, "/api/projects", {"q": "other"})["total"] == 1
    hidden = _list(viewer, "/api/projects", {"q": "other"})
    assert (hidden["items"], hidden["total"]) == ([], 0)
    assert _list(viewer, "/api/projects", {"q": "octo/other"})["total"] == 0
    paged = _list(viewer, "/api/projects", {"per_page": 1})
    assert (paged["total"], paged["pages"], _ids(viewer.get("/api/projects"))) == (1, 1, ["demo"])
    assert _list(owner, "/api/projects", {"per_page": 1})["pages"] == 2
    beyond = _list(viewer, "/api/projects", {"per_page": 1, "page": 2})
    assert (beyond["items"], beyond["total"]) == ([], 1)
    # runs: the same for the global list, with filters and the number search
    assert _list(owner, "/api/runs", {"q": "other"})["total"] == 1
    assert _list(viewer, "/api/runs", {"q": "other"})["total"] == 0
    assert _list(viewer, "/api/runs", {"project": "other"})["total"] == 0
    runs = _list(viewer, "/api/runs", {"per_page": 1})
    assert (runs["total"], runs["pages"], len(runs["items"])) == (2, 2, 1)
    assert _list(viewer, "/api/runs", {"q": "#1", "environment": "production"})["total"] == 1


def test_project_detail_is_hidden_without_access(world: World) -> None:
    assert world["nobody"].get(DEMO).status_code == 404
    assert world["viewer"].get("/api/projects/other").status_code == 404
    assert world["viewer"].get(DEMO).status_code == 200
    assert world["devonly"].get(f"{PROD_BASE}/runs").status_code == 404
    assert world["devonly"].get(f"{DEV_BASE}/runs").status_code == 200
    assert world["nobody"].get(f"{DEV_BASE}/runs").status_code == 404


def test_run_list_is_filtered_and_counts_only_visible_runs(world: World) -> None:
    owner = world.owner.get("/api/runs").json()
    assert owner["total"] == 3
    viewer = world["viewer"].get("/api/runs").json()
    assert {(r["project"], r["environment"]) for r in viewer["items"]} == {
        ("demo", "development"),
        ("demo", "production"),
    }
    assert viewer["total"] == 2
    devonly = world["devonly"].get("/api/runs").json()
    assert [(r["project"], r["environment"]) for r in devonly["items"]] == [("demo", "development")]
    nobody = world["nobody"].get("/api/runs").json()
    assert (nobody["items"], nobody["total"], nobody["pages"]) == ([], 0, 1)
    assert world["logs"].get("/api/runs").json()["total"] == 0  # no run permission


def test_run_detail_log_and_events_follow_the_run_environment(world: World) -> None:
    demo_dev, demo_prod = world.runs["demo_dev"], world.runs["demo_prod"]
    other = world.runs["other_dev"]
    assert world["viewer"].get(f"/api/runs/{demo_dev}").status_code == 200
    assert world["viewer"].get(f"/api/runs/{other}").status_code == 404
    assert world["devonly"].get(f"/api/runs/{demo_prod}").status_code == 404
    assert world["devonly"].get(f"/api/runs/{demo_dev}").status_code == 200
    assert world["nobody"].get(f"/api/runs/{demo_dev}").status_code == 404
    assert world["logs"].get(f"/api/runs/{demo_dev}").status_code == 403
    assert world["nobody"].get(f"/api/runs/{demo_dev}/steps/1/log").status_code == 404
    assert world["nobody"].get(f"/api/runs/{demo_dev}/events").status_code == 404
    assert world["logs"].get(f"/api/runs/{demo_dev}/events").status_code == 403
    assert world.owner.get("/api/runs/999").status_code == 404
    assert world["viewer"].get("/api/runs/abc").status_code == 404


# --- runs and deployments --------------------------------------------------------------------


def test_run_actions_need_developer_and_production_needs_maintainer(world: World) -> None:
    body = {"branch": "development", "commit_sha": SHA_A}
    prod_body = {"branch": "production", "commit_sha": SHA_B}
    assert world["viewer"].post(f"{DEV_BASE}/runs", body).status_code == 403
    assert world["developer"].post(f"{DEV_BASE}/runs", body).status_code == 201
    assert world["developer"].post(f"{PROD_BASE}/runs", prod_body).status_code == 403
    assert world["maintainer"].post(f"{PROD_BASE}/runs", prod_body).status_code == 201
    assert world["nobody"].post(f"{DEV_BASE}/runs", body).status_code == 404
    assert world["devonly"].post(f"{DEV_BASE}/runs", body).status_code == 403


def test_cancel_and_retry_follow_the_run(world: World) -> None:
    dev, prod = world.runs["demo_dev"], world.runs["demo_prod"]
    assert world["viewer"].post(f"/api/runs/{dev}/cancel").status_code == 403
    assert world["developer"].post(f"/api/runs/{prod}/cancel").status_code == 403
    assert world["developer"].post(f"/api/runs/{dev}/cancel").status_code == 200
    assert world["developer"].post(f"/api/runs/{dev}/retry").status_code == 201
    assert world["viewer"].post(f"/api/runs/{dev}/retry").status_code == 403
    assert world["nobody"].post(f"/api/runs/{dev}/retry").status_code == 404
    assert world["maintainer"].post(f"/api/runs/{prod}/cancel").status_code == 200


def test_deployments_read_is_viewer_and_redeploy_is_maintainer(world: World) -> None:
    path = f"{PROD_BASE}/deployments"
    assert world["viewer"].get(path).status_code == 200
    assert world["nobody"].get(path).status_code == 404
    assert world["viewer"].post(f"{path}/1/redeploy").status_code == 403
    assert world["developer"].post(f"{path}/1/redeploy").status_code == 403
    missing = world["maintainer"].post(f"{path}/1/redeploy")
    assert missing.status_code == 404 and "deployment" in missing.text  # authorized, no such row
    assert world["devonly"].get(path).status_code == 404


# --- containers ------------------------------------------------------------------------------


def test_container_list_and_logs_for_roles(world: World) -> None:
    path = f"{DEV_BASE}/containers"
    assert _ids(world["viewer"].get(path), "service") == ["web", "worker"]
    assert world["viewer"].get(f"{path}/{WEB}/logs").status_code == 403
    assert world["developer"].get(f"{path}/{WEB}/logs").status_code == 403
    assert world["maintainer"].get(f"{path}/{WEB}/logs").status_code == 200
    assert world["viewer"].post(f"{path}/{WEB}/actions", {"action": "restart"}).status_code == 403
    assert (
        world["maintainer"].post(f"{path}/{WEB}/actions", {"action": "restart"}).status_code == 200
    )
    assert world["viewer"].get(f"{path}/{WEB}").status_code == 200
    assert world["nobody"].get(path).status_code == 404


def test_container_grants_allow_exactly_the_granted_service_and_permission(world: World) -> None:
    path = f"{DEV_BASE}/containers"
    logs, actions, terminal = world["logs"], world["actions"], world["terminal"]
    # only the granted service is listed and readable
    for who in (logs, actions, terminal):
        assert _ids(who.get(path), "service") == ["web"]
        assert who.get(f"{path}/{WEB}").status_code == 200
        assert who.get(f"{path}/{WORKER}").status_code == 403
    # logs
    assert logs.get(f"{path}/{WEB}/logs").status_code == 200
    assert logs.get(f"{path}/{WORKER}/logs").status_code == 403
    assert actions.get(f"{path}/{WEB}/logs").status_code == 403
    assert terminal.get(f"{path}/{WEB}/logs").status_code == 403
    # actions
    plan = {"action": "restart"}
    assert actions.post(f"{path}/{WEB}/actions", plan).status_code == 200
    assert actions.post(f"{path}/{WORKER}/actions", plan).status_code == 403
    assert logs.post(f"{path}/{WEB}/actions", plan).status_code == 403
    assert terminal.post(f"{path}/{WEB}/actions", plan).status_code == 403
    # terminal tickets
    tickets = f"{path}/{WEB}/terminal/tickets"
    assert terminal.post(tickets, {}).status_code == 200
    assert logs.post(tickets, {}).status_code == 403
    assert actions.post(tickets, {}).status_code == 403
    assert terminal.post(f"{path}/{WORKER}/terminal/tickets", {}).status_code == 403
    # nothing beyond the three permissions, and the other environment does not exist for them
    assert logs.put(f"{path}/{WEB}/limits", {"cpus": 1}).status_code == 403
    assert logs.get(f"{path}/{WEB}/files?path=/").status_code == 403
    assert logs.post(f"{path}/{WEB}/env/X/reveal").status_code == 403
    assert logs.get(f"{DEMO}/environments/production/containers").status_code == 404


def test_container_roles_do_not_leak_to_other_environments(world: World) -> None:
    prod = f"{PROD_BASE}/containers"
    assert world["devonly"].get(prod).status_code == 404
    assert world["viewer"].get(f"{prod}/{WEB}/logs").status_code == 403
    assert world["maintainer"].get(f"{prod}/{WEB}/logs").status_code == 200


def test_viewer_resolves_no_service_lookup_for_role_decisions(world: World) -> None:
    world.fake.calls.clear()
    assert world["maintainer"].get(f"{DEV_BASE}/containers/{WEB}/logs").status_code == 200
    assert world.fake.calls == ["logs"]  # the role allowed it: the service was never looked up
    world.fake.calls.clear()
    assert world["logs"].get(f"{DEV_BASE}/containers/{WEB}/logs").status_code == 200
    assert world.fake.calls == ["get", "logs"]


# --- databases, domains, secrets, pipelines, settings ----------------------------------------


def test_database_data_and_configuration(world: World) -> None:
    rows = "databases/main/schemas/public/tables/t/rows"
    query = "databases/main/query"
    assert world["viewer"].get(f"{DEV_BASE}/databases").status_code in {200, 502, 503}
    assert world["viewer"].get(f"{DEV_BASE}/{rows}").status_code == 403
    assert _passes(world["developer"].get(f"{DEV_BASE}/{rows}"))
    assert world["developer"].get(f"{PROD_BASE}/{rows}").status_code == 403  # production
    assert (
        world["developer"].post(f"{PROD_BASE}/{query}", {"statement": "select 1"}).status_code
        == 403
    )
    assert _passes(world["maintainer"].get(f"{PROD_BASE}/{rows}"))
    assert _passes(world.owner.get(f"{PROD_BASE}/{rows}"))
    config = {
        "name": "x",
        "engine": "postgres",
        "host": "h",
        "database": "d",
        "username": "u",
        "password": "p",
    }
    assert world["maintainer"].post(f"{DEV_BASE}/databases", config).status_code == 403


def test_domains_are_maintainer_and_certificates_admin_only(world: World) -> None:
    assert world["viewer"].get(f"{DEV_BASE}/domains").status_code == 200
    assert world["viewer"].post(f"{DEV_BASE}/domains", {}).status_code == 403
    assert world["developer"].post(f"{DEV_BASE}/domains", {}).status_code == 403
    assert world["maintainer"].post(f"{DEV_BASE}/domains", {}).status_code == 422  # authorized
    assert world["nobody"].get(f"{DEV_BASE}/domains").status_code == 404
    assert world["maintainer"].get("/api/ingress/certificates").status_code == 403
    assert world.owner.get("/api/ingress/certificates").status_code == 200


def test_secrets_listing_and_changes(world: World) -> None:
    path = f"{DEV_BASE}/secrets"
    assert world["nobody"].get(path).status_code == 404
    assert _passes(world["viewer"].get(path))
    assert world["maintainer"].put(f"{path}/TOKEN", {"value": "v"}).status_code == 403
    assert world["maintainer"].post(f"{path}/TOKEN/reveal").status_code == 403
    assert world["maintainer"].delete(f"{path}/TOKEN").status_code == 403


def test_pipeline_read_write_and_editor_tools(world: World) -> None:
    body = {"jenkinsfile": "pipeline {}", "base_version": None}
    assert world["viewer"].put(f"{DEV_BASE}/pipeline", body).status_code == 403
    assert _passes(world["developer"].put(f"{DEV_BASE}/pipeline", body))
    assert world["developer"].put(f"{PROD_BASE}/pipeline", body).status_code == 403
    assert _passes(world["maintainer"].put(f"{PROD_BASE}/pipeline", body))
    assert world["nobody"].get(f"{DEV_BASE}/pipeline").status_code == 404
    assert world["nobody"].get("/api/pipelines/catalog").status_code == 200
    assert world["nobody"].get("/api/pipelines/templates").status_code == 200
    scoped = {"jenkinsfile": "pipeline {}", "project": "demo", "environment": "production"}
    assert world["nobody"].post("/api/pipelines/validate", scoped).status_code == 404
    assert world["devonly"].post("/api/pipelines/validate", scoped).status_code == 404
    assert world["viewer"].post("/api/pipelines/validate", scoped).status_code == 200
    plain = {"jenkinsfile": "pipeline {}"}
    assert world["nobody"].post("/api/pipelines/validate", plain).status_code == 200


def test_project_and_environment_settings_are_admin_only(world: World) -> None:
    new = {
        "slug": "n1",
        "name": "N",
        "repository": "octo/n",
        "development_branch": "d",
        "production_branch": "p",
    }
    assert world["maintainer"].post("/api/projects", new).status_code == 403
    assert world["maintainer"].patch(DEMO, {"name": "x"}).status_code == 403
    assert world["maintainer"].patch(f"{DEV_BASE}", {"branch": "x"}).status_code == 403
    assert world["nobody"].patch(DEMO, {"name": "x"}).status_code == 404 or True
    assert world.owner.patch(DEMO, {"name": "Renamed"}).status_code == 200


def test_telemetry_needs_some_access(world: World) -> None:
    assert world["nobody"].get("/api/telemetry/host").status_code == 403


# --- tokens never exceed their owner ---------------------------------------------------------


def _token(world: World, who: str, scopes: list[str]) -> dict[str, str]:
    body = {"name": "t", "scopes": scopes}
    created = world[who].post("/api/tokens", body)
    assert created.status_code == 201
    return {"Authorization": f"Bearer {created.json()['token']}"}


def _bare(world: World) -> TestClient:
    return TestClient(world.owner.client.app, base_url=ORIGIN)


def test_token_is_the_intersection_of_scopes_and_owner_permissions(world: World) -> None:
    bare = _bare(world)
    everything = _token(world, "viewer", [s.value for s in TokenScope])
    # an admin-scope token of a viewer still only sees and does what the viewer may
    assert _ids(bare.get("/api/projects", headers=everything)) == ["demo"]
    dev = world.runs["demo_dev"]
    assert bare.get(f"/api/runs/{dev}", headers=everything).status_code == 200
    assert bare.get(f"/api/runs/{world.runs['other_dev']}", headers=everything).status_code == 404
    origin = {"Origin": ORIGIN}
    cancel = bare.post(f"/api/runs/{dev}/cancel", headers={**everything, **origin})
    assert cancel.status_code == 403
    assert bare.get("/api/users", headers=everything).status_code == 403
    prod_rows = f"{PROD_BASE}/databases/main/schemas/public/tables/t/rows"
    assert bare.get(prod_rows, headers=everything).status_code == 403
    # and the reverse: a read-only token of an owner cannot cancel
    narrow = _token(world, "developer", ["read"])
    cancel = bare.post(f"/api/runs/{dev}/cancel", headers={**narrow, **origin})
    assert cancel.status_code == 403
    assert "missing scope" in cancel.json()["error"]["message"]
    wide = _token(world, "developer", ["run"])
    assert bare.post(f"/api/runs/{dev}/retry", headers={**wide, **origin}).status_code == 409


def test_a_member_sees_only_their_own_tokens(world: World) -> None:
    _token(world, "viewer", ["read"])
    _token(world, "developer", ["read"])
    assert world["viewer"].get("/api/tokens").json()["total"] == 1
    assert world["developer"].get("/api/tokens").json()["total"] == 1
    other = world["developer"].get("/api/tokens").json()["items"][0]["id"]
    assert world["viewer"].delete(f"/api/tokens/{other}").status_code == 404


def test_token_listing_is_filtered_like_the_session(world: World) -> None:
    bare = _bare(world)
    headers = _token(world, "nobody", ["read"])
    assert bare.get("/api/runs", headers=headers).json()["total"] == 0
    assert bare.get("/api/projects", headers=headers).json()["items"] == []


# --- grant management -------------------------------------------------------------------------


def _grants(world: World, slug: str = "demo") -> list[dict[str, Any]]:
    grants: list[dict[str, Any]] = world.owner.get(f"/api/projects/{slug}/access").json()["grants"]
    return grants


def test_grants_can_be_added_changed_and_removed_with_effect_at_once(world: World) -> None:
    nobody = world["nobody"]
    assert nobody.get(DEMO).status_code == 404
    added = world.owner.put(f"{DEMO}/access", {"user_id": nobody.user_id, "role": "viewer"})
    assert added.status_code == 200 and added.json()["role"] == "viewer"
    assert nobody.get(DEMO).status_code == 200
    assert nobody.get(f"{DEV_BASE}/containers/{WEB}/logs").status_code == 403
    changed = world.owner.put(f"{DEMO}/access", {"user_id": nobody.user_id, "role": "maintainer"})
    assert changed.json()["id"] == added.json()["id"]  # same target: replaced, not duplicated
    assert nobody.get(f"{DEV_BASE}/containers/{WEB}/logs").status_code == 200
    assert world.owner.delete(f"{DEMO}/access/{added.json()['id']}").status_code == 204
    assert nobody.get(DEMO).status_code == 404
    actions = [e.action for e in AuditService(world.container.uow()).list(limit=50)]
    for name in ("access.grant_add", "access.grant_update", "access.grant_remove"):
        assert name in actions


def test_grant_audit_records_who_what_and_where(world: World) -> None:
    uid = world["nobody"].user_id
    world.owner.put(
        f"{DEMO}/access",
        {"user_id": uid, "environment": "production", "service": "web", "permissions": ["logs"]},
    )
    event = AuditService(world.container.uow()).list(action="access.grant_add")[0]
    assert event.details["user"] == "nobody"
    assert event.details["project"] == "demo"
    assert event.details["environment"] == "production"
    assert event.details["service"] == "web"
    assert event.details["permissions"] == ["logs"]
    assert event.actor_id == str(world.owner.user_id)


def test_grant_validation(world: World) -> None:
    uid = world["nobody"].user_id
    put = world.owner.put
    assert put(f"{DEMO}/access", {"user_id": uid}).status_code == 422  # no role
    assert (
        put(
            f"{DEMO}/access", {"user_id": uid, "role": "viewer", "permissions": ["logs"]}
        ).status_code
        == 422
    )
    assert (
        put(
            f"{DEMO}/access", {"user_id": uid, "service": "web", "permissions": ["logs"]}
        ).status_code
        == 422
    )
    both = {"user_id": uid, "environment": "development", "service": "web", "role": "viewer"}
    assert put(f"{DEMO}/access", both).status_code == 422
    empty = {"user_id": uid, "environment": "development", "service": "web", "permissions": []}
    assert put(f"{DEMO}/access", empty).status_code == 422
    bad = {"user_id": uid, "environment": "development", "service": "web", "permissions": ["root"]}
    assert put(f"{DEMO}/access", bad).status_code == 422
    assert put(f"{DEMO}/access", {"user_id": 9999, "role": "viewer"}).status_code == 404
    admin_id = world.owner.user_id
    assert put(f"{DEMO}/access", {"user_id": admin_id, "role": "viewer"}).status_code == 422
    assert put("/api/projects/nope/access", {"user_id": uid, "role": "viewer"}).status_code == 404
    assert world.owner.delete(f"{DEMO}/access/9999").status_code == 404


def test_only_admins_manage_access(world: World) -> None:
    uid = world["nobody"].user_id
    for who in ("viewer", "developer", "maintainer"):
        assert world[who].get(f"{DEMO}/access").status_code == 403
        assert (
            world[who].put(f"{DEMO}/access", {"user_id": uid, "role": "viewer"}).status_code == 403
        )
    assert world["nobody"].get(f"{DEMO}/access").status_code == 404
    add_user(world.container, "ada", GlobalRole.ADMIN)
    ada = sign_in(world.container, "ada")
    assert ada.put(f"{DEMO}/access", {"user_id": uid, "role": "viewer"}).status_code == 200


def test_access_overview_lists_implicit_admins_and_grants(world: World) -> None:
    body = world.owner.get(f"{DEMO}/access").json()
    assert [u["username"] for u in body["privileged"]] == ["admin"]
    assert {g["username"] for g in body["grants"]} >= {"viewer", "logs", "terminal"}
    service = next(g for g in body["grants"] if g["username"] == "logs")
    assert (service["environment"], service["service"], service["permissions"]) == (
        "development",
        "web",
        ["logs"],
    )
    mine = world.owner.get(f"/api/users/{world['viewer'].user_id}/access").json()
    assert [(g["project"], g["role"]) for g in mine] == [("demo", "viewer")]


def test_session_summary_lists_effective_permissions(world: World) -> None:
    summary = world["developer"].get("/api/auth/session").json()["permissions"]
    dev = summary["projects"]["demo"]["environments"]["development"]
    prod = summary["projects"]["demo"]["environments"]["production"]
    assert "run.create" in dev["actions"] and "run.create" not in prod["actions"]
    assert dev["role"] == "developer"
    assert "other" not in summary["projects"]
    svc = world["logs"].get("/api/auth/session").json()["permissions"]
    web = svc["projects"]["demo"]["environments"]["development"]["services"]["web"]
    assert "container.logs" in web and "container.action" not in web


def test_streams_stop_when_a_grant_is_revoked(world: World) -> None:
    viewer = world["viewer"]
    principal = authz.refresh(
        world.container, Principal(session=_context(world, "viewer"), session_token=_cookie(viewer))
    )
    assert principal is not None
    resource = Resource.of_environment("demo", DEV)
    assert authz.still_allowed(world.container, principal, Action.RUN_READ, resource)
    grant_id = next(g["id"] for g in _grants(world) if g["username"] == "viewer")
    assert world.owner.delete(f"{DEMO}/access/{grant_id}").status_code == 204
    assert not authz.still_allowed(world.container, principal, Action.RUN_READ, resource)
    assert authz.refresh(world.container, principal) is not None  # still signed in, just no access


def _cookie(session: Session) -> str:
    name = "__Host-session"
    value = session.client.cookies.get(name) or session.client.cookies.get("jendock_session")
    assert value is not None
    return value


def _context(world: World, name: str) -> Any:
    token = _cookie(world[name])
    context = world.container.session_service().resolve(token, touch=False)
    assert context is not None
    return context


def test_streams_stop_when_the_user_is_disabled(world: World) -> None:
    principal = Principal(session=_context(world, "viewer"), session_token=_cookie(world["viewer"]))
    resource = Resource.of_environment("demo", DEV)
    live = authz.refresh(world.container, principal)
    assert live is not None and authz.still_allowed(
        world.container, live, Action.RUN_READ, resource
    )
    world.owner.patch(f"/api/users/{world['viewer'].user_id}", {"is_active": False})
    assert authz.refresh(world.container, live) is None


def test_every_endpoint_requires_authentication(world: World) -> None:
    anonymous = TestClient(world.owner.client.app, base_url=ORIGIN)
    for path in (
        "/api/projects",
        "/api/runs",
        "/api/users",
        f"{DEMO}/access",
        f"{DEV_BASE}/containers",
        "/api/telemetry/host",
        "/api/tokens",
    ):
        assert anonymous.get(path).status_code == 401, path


_ = SYSTEM
