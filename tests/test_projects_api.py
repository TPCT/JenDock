"""Projects, environment settings, run lists, branch heads, manual runs and retries."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from backend.api.container import AppContainer
from backend.domain.enums import (
    EnvironmentName,
    PipelineSource,
    RunStatus,
    RunTrigger,
)
from backend.repositories.models import Run
from backend.services.audit import AuditService
from backend.services.projects import ProjectService
from backend.services.runs import RunService
from tests.api_support import (
    FakeClock,
    create_user,
    login,
    make_client,
    make_container,
    make_settings,
    mutation_headers,
)
from tests.conftest import DEV, PROD
from tests.github_support import SHA_A, SHA_B, FakeGithub

ORIGIN = "https://app.example.test"


@pytest.fixture
def github() -> FakeGithub:
    return FakeGithub({"development": SHA_A, "production": SHA_B, "feature/x": SHA_B})


@pytest.fixture
def wired(
    tmp_path: Path, engine: Engine, clock: FakeClock, github: FakeGithub, project: str
) -> AppContainer:
    return make_container(make_settings(tmp_path), engine, clock, github=github)


@pytest.fixture
def api(wired: AppContainer) -> tuple[TestClient, str]:
    create_user(wired)
    client = make_client(wired)
    return client, login(client)


def _bearer(client: TestClient, csrf: str, *scopes: str) -> dict[str, str]:
    body = {"name": "agent", "scopes": list(scopes)}
    created = client.post("/api/tokens", json=body, headers=mutation_headers(client, csrf))
    return {"Authorization": f"Bearer {created.json()['token']}"}


def _anonymous(client: TestClient) -> TestClient:
    return TestClient(client.app, base_url=str(client.base_url))


def _queue(
    wired: AppContainer, env: EnvironmentName = DEV, sha: str = SHA_A, branch: str = "development"
) -> Run:
    return RunService(wired.uow(), wired.clock).create_queued(
        "demo",
        env,
        trigger=RunTrigger.PUSH,
        branch=branch,
        commit_sha=sha,
        pipeline_source=PipelineSource.PLATFORM,
        pipeline_text="",
    )


def _finish(wired: AppContainer, status: RunStatus = RunStatus.FAILED) -> int:
    lifecycle = wired.run_lifecycle()
    run = lifecycle.claim_next()
    assert run is not None
    assert lifecycle.finish(run.id, status, "boom")
    return run.id


# --- project reads ---------------------------------------------------------------------------


def test_list_projects_shape_and_order(wired: AppContainer, api: tuple[TestClient, str]) -> None:
    client, _ = api
    ProjectService(wired.uow()).create("aaa", "Zed Project", "octo/zed")
    ProjectService(wired.uow()).create("zzz", "Alpha Project", "octo/alpha")
    body = client.get("/api/projects").json()
    assert [p["name"] for p in body] == ["Alpha Project", "Demo", "Zed Project"]
    demo = body[1]
    assert demo["slug"] == "demo"
    assert demo["repository"] == "octo/demo"
    assert demo["git_transport"] == "https"  # fixture project, created with the default
    assert demo["created_at"].endswith("Z") or "+00:00" in demo["created_at"]
    assert [(e["name"], e["branch"], e["auto_deploy"]) for e in demo["environments"]] == [
        ("development", "development", False),
        ("production", "production", False),
    ]
    assert all(e["last_run"] is None for e in demo["environments"])


def test_last_run_is_the_newest_by_creation_time(
    wired: AppContainer, clock: FakeClock, api: tuple[TestClient, str]
) -> None:
    client, _ = api
    _queue(wired)
    clock.advance(minutes=5)
    newest = _queue(wired, sha=SHA_B)
    clock.advance(minutes=5)
    _queue(wired, PROD, branch="production")
    environments = client.get("/api/projects/demo").json()["environments"]
    dev, prod = environments
    assert dev["last_run"]["id"] == newest.id
    assert dev["last_run"]["commit_sha"] == SHA_B
    assert dev["last_run"]["project"] == "demo"
    assert "stages" not in dev["last_run"]
    assert prod["last_run"]["environment"] == "production"


def test_archived_projects_are_omitted_and_not_found(
    wired: AppContainer, api: tuple[TestClient, str]
) -> None:
    client, _ = api
    ProjectService(wired.uow()).archive("demo")
    assert client.get("/api/projects").json() == []
    assert client.get("/api/projects/demo").status_code == 404


def test_unknown_project_is_404(api: tuple[TestClient, str]) -> None:
    client, _ = api
    response = client.get("/api/projects/missing")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_reads_need_authentication_and_the_read_scope(api: tuple[TestClient, str]) -> None:
    client, csrf = api
    bare = _anonymous(client)
    for path in (
        "/api/projects",
        "/api/projects/demo",
        "/api/projects/demo/environments/development/runs",
        "/api/projects/demo/environments/development/head?branch=development",
        "/api/runs",
    ):
        assert bare.get(path).status_code == 401, path
        no_read = _bearer(client, csrf, "run")
        assert bare.get(path, headers=no_read).status_code == 403, path
        reader = _bearer(client, csrf, "read")
        assert bare.get(path, headers=reader).status_code == 200, path


# --- run lists -------------------------------------------------------------------------------


def test_environment_runs_are_paged_newest_first(
    wired: AppContainer, clock: FakeClock, api: tuple[TestClient, str]
) -> None:
    client, _ = api
    ids = []
    for _n in range(5):
        ids.append(_queue(wired).id)
        clock.advance(seconds=1)
    _queue(wired, PROD, branch="production")
    page = client.get("/api/projects/demo/environments/development/runs?limit=2").json()
    assert page["total"] == 5
    assert (page["limit"], page["offset"]) == (2, 0)
    assert [r["id"] for r in page["items"]] == [ids[4], ids[3]]
    second = client.get(
        "/api/projects/demo/environments/development/runs", params={"limit": 2, "offset": 4}
    ).json()
    assert [r["id"] for r in second["items"]] == [ids[0]]
    defaults = client.get("/api/projects/demo/environments/development/runs").json()
    assert (defaults["limit"], len(defaults["items"])) == (20, 5)
    prod = client.get("/api/projects/demo/environments/production/runs").json()
    assert prod["total"] == 1


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "offset=-1", "limit=x"])
def test_run_list_bounds(api: tuple[TestClient, str], query: str) -> None:
    client, _ = api
    assert client.get(f"/api/runs?{query}").status_code == 422
    assert (
        client.get(f"/api/projects/demo/environments/development/runs?{query}").status_code == 422
    )


def test_unknown_project_or_environment_runs(api: tuple[TestClient, str]) -> None:
    client, _ = api
    assert client.get("/api/projects/nope/environments/development/runs").status_code == 404
    assert client.get("/api/projects/demo/environments/staging/runs").status_code == 422


def test_global_runs_span_projects(
    wired: AppContainer, clock: FakeClock, api: tuple[TestClient, str]
) -> None:
    client, _ = api
    ProjectService(wired.uow()).create("other", "Other", "octo/other")
    first = _queue(wired)
    clock.advance(seconds=1)
    second = RunService(wired.uow(), wired.clock).create_queued(
        "other",
        DEV,
        trigger=RunTrigger.MANUAL,
        branch="development",
        commit_sha=SHA_B,
        pipeline_source=PipelineSource.PLATFORM,
        pipeline_text="",
    )
    page = client.get("/api/runs").json()
    assert page["total"] == 2
    assert [(r["id"], r["project"]) for r in page["items"]] == [
        (second.id, "other"),
        (first.id, "demo"),
    ]
    assert set(page["items"][0]) == {
        "id",
        "number",
        "project",
        "environment",
        "trigger",
        "branch",
        "commit_sha",
        "pipeline_source",
        "status",
        "message",
        "cancel_requested_at",
        "created_at",
        "started_at",
        "finished_at",
    }


# --- create project --------------------------------------------------------------------------


def test_create_project_with_defaults_and_audit(
    wired: AppContainer, api: tuple[TestClient, str]
) -> None:
    client, csrf = api
    body = {"slug": "shop", "name": "Shop", "repository": "octo/shop"}
    response = client.post("/api/projects", json=body, headers=mutation_headers(client, csrf))
    assert response.status_code == 201
    data = response.json()
    assert data["git_transport"] == "ssh"
    assert [(e["name"], e["branch"], e["auto_deploy"]) for e in data["environments"]] == [
        ("development", "development", False),
        ("production", "production", False),
    ]
    [event] = AuditService(wired.uow()).list(action="project.create")
    assert event.target_id == "shop"
    assert event.details["repository"] == "octo/shop"


def test_create_project_with_branches_and_transport(api: tuple[TestClient, str]) -> None:
    client, csrf = api
    body = {
        "slug": "shop",
        "name": "Shop",
        "repository": "octo/shop",
        "development_branch": "develop",
        "production_branch": "main",
        "git_transport": "https",
    }
    response = client.post("/api/projects", json=body, headers=mutation_headers(client, csrf))
    data = response.json()
    assert [e["branch"] for e in data["environments"]] == ["develop", "main"]
    assert data["git_transport"] == "https"
    assert client.get("/api/projects/shop").json() == data


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ({"slug": "demo", "name": "Dup", "repository": "octo/dup"}, 409),
        ({"slug": "Bad Slug", "name": "N", "repository": "octo/x"}, 422),
        ({"slug": "ok", "name": "N", "repository": "not a repo"}, 422),
        ({"slug": "ok", "name": "N", "repository": "octo/x", "development_branch": "a..b"}, 422),
        (
            {
                "slug": "ok",
                "name": "N",
                "repository": "octo/x",
                "development_branch": "m",
                "production_branch": "m",
            },
            422,
        ),
        ({"slug": "ok", "name": "N", "repository": "octo/x", "git_transport": "ftp"}, 422),
        ({"slug": "ok", "name": "N", "repository": "octo/x", "extra": 1}, 422),
        ({"slug": "ok"}, 422),
    ],
)
def test_create_project_rejections(
    api: tuple[TestClient, str], body: dict[str, object], status: int
) -> None:
    client, csrf = api
    response = client.post("/api/projects", json=body, headers=mutation_headers(client, csrf))
    assert response.status_code == status


def test_create_project_scopes_and_csrf(api: tuple[TestClient, str]) -> None:
    client, csrf = api
    body = {"slug": "shop", "name": "Shop", "repository": "octo/shop"}
    assert client.post("/api/projects", json=body).status_code == 403  # no Origin, no CSRF
    assert client.post("/api/projects", json=body, headers={"Origin": ORIGIN}).status_code == 403
    wrong = {"Origin": ORIGIN, "X-CSRF-Token": "nope"}
    assert client.post("/api/projects", json=body, headers=wrong).status_code == 403
    bare = _anonymous(client)
    assert bare.post("/api/projects", json=body).status_code == 401
    for scopes in (["read"], ["run"], ["pipelines"]):
        headers = _bearer(client, csrf, *scopes)
        assert bare.post("/api/projects", json=body, headers=headers).status_code == 403
    admin = _bearer(client, csrf, "admin")
    assert bare.post("/api/projects", json=body, headers=admin).status_code == 201


# --- environment and project settings --------------------------------------------------------


ENV_URL = "/api/projects/demo/environments"


def test_patch_environment_branch_and_auto_deploy(
    wired: AppContainer, api: tuple[TestClient, str]
) -> None:
    client, csrf = api
    headers = mutation_headers(client, csrf)
    response = client.patch(
        f"{ENV_URL}/development", json={"branch": "develop", "auto_deploy": True}, headers=headers
    )
    assert response.status_code == 200
    assert response.json() == {
        "name": "development",
        "branch": "develop",
        "auto_deploy": True,
        "compose_project": None,
        "compose_files": [],
        "compose_env_files": [],
        "services": [],
        "has_pipeline": False,
        "last_run": None,
    }
    only = client.patch(f"{ENV_URL}/development", json={"auto_deploy": False}, headers=headers)
    assert (only.json()["branch"], only.json()["auto_deploy"]) == ("develop", False)
    stored = client.get("/api/projects/demo").json()["environments"][0]
    assert (stored["branch"], stored["auto_deploy"]) == ("develop", False)
    events = AuditService(wired.uow()).list(action="environment.update")
    assert len(events) == 2
    assert events[1].details["branch"] == {"from": "development", "to": "develop"}


def test_patch_environment_compose_settings_round_trip_and_clear(
    wired: AppContainer, api: tuple[TestClient, str]
) -> None:
    client, csrf = api
    headers = mutation_headers(client, csrf)
    body = {
        "compose_project": "demo-dev",
        "compose_files": ["/srv/demo/compose.yml", "/srv/demo/extra.yml"],
        "compose_env_files": ["/srv/demo/ports.env"],
        "services": ["web", "worker"],
    }
    response = client.patch(f"{ENV_URL}/development", json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert {k: response.json()[k] for k in body} == body
    shown = client.get("/api/projects/demo").json()["environments"][0]
    assert shown["compose_project"] == "demo-dev"
    assert shown["compose_env_files"] == ["/srv/demo/ports.env"]
    untouched = client.patch(f"{ENV_URL}/development", json={"auto_deploy": True}, headers=headers)
    assert untouched.json()["compose_project"] == "demo-dev"  # not named: unchanged
    cleared = client.patch(
        f"{ENV_URL}/development", json={"compose_project": None}, headers=headers
    )
    assert cleared.json()["compose_project"] is None
    assert cleared.json()["services"] == ["web", "worker"]
    event = AuditService(wired.uow()).list(action="environment.update")[0]
    assert event.details["compose"] == {
        "project": None,
        "files": ["/srv/demo/compose.yml", "/srv/demo/extra.yml"],
        "env_files": ["/srv/demo/ports.env"],
        "services": ["web", "worker"],
    }
    assert client.get("/api/projects/demo").json()["environments"][1]["compose_project"] is None


def test_patch_environment_includes_the_last_run(
    wired: AppContainer, api: tuple[TestClient, str]
) -> None:
    client, csrf = api
    run = _queue(wired)
    response = client.patch(
        f"{ENV_URL}/development", json={"auto_deploy": True}, headers=mutation_headers(client, csrf)
    )
    assert response.json()["last_run"]["id"] == run.id


@pytest.mark.parametrize(
    ("path", "body", "status"),
    [
        ("development", {}, 422),
        ("development", {"branch": "a..b"}, 422),
        ("development", {"branch": "production"}, 422),  # the other environment's branch
        ("development", {"branch": ""}, 422),
        ("development", {"auto_deploy": "yes please"}, 422),
        ("development", {"compose_project": "Not Valid"}, 422),
        ("development", {"compose_files": ["relative.yml"]}, 422),
        ("development", {"compose_env_files": ["/a/../b.env"]}, 422),
        ("development", {"services": ["ok", "bad name"]}, 422),
        ("development", {"services": None}, 422),
        ("development", {"unknown": 1}, 422),
        ("staging", {"auto_deploy": True}, 422),
    ],
)
def test_patch_environment_rejections(
    api: tuple[TestClient, str], path: str, body: dict[str, object], status: int
) -> None:
    client, csrf = api
    response = client.patch(f"{ENV_URL}/{path}", json=body, headers=mutation_headers(client, csrf))
    assert response.status_code == status


def test_patch_unknown_project_is_404(api: tuple[TestClient, str]) -> None:
    client, csrf = api
    response = client.patch(
        "/api/projects/none/environments/development",
        json={"auto_deploy": True},
        headers=mutation_headers(client, csrf),
    )
    assert response.status_code == 404


def test_patch_environment_scopes_and_csrf(api: tuple[TestClient, str]) -> None:
    client, csrf = api
    body = {"auto_deploy": True}
    url = f"{ENV_URL}/development"
    assert client.patch(url, json=body).status_code == 403
    assert client.patch(url, json=body, headers={"Origin": ORIGIN}).status_code == 403
    bare = _anonymous(client)
    assert bare.patch(url, json=body).status_code == 401
    for scopes in (["read"], ["run"], ["deploy"], ["pipelines"]):
        headers = _bearer(client, csrf, *scopes)
        assert bare.patch(url, json=body, headers=headers).status_code == 403
    assert bare.patch(url, json=body, headers=_bearer(client, csrf, "admin")).status_code == 200


def test_patch_project_transport(wired: AppContainer, api: tuple[TestClient, str]) -> None:
    client, csrf = api
    headers = mutation_headers(client, csrf)
    response = client.patch("/api/projects/demo", json={"git_transport": "ssh"}, headers=headers)
    assert response.status_code == 200
    assert response.json()["git_transport"] == "ssh"
    assert client.patch("/api/projects/demo", json={}, headers=headers).status_code == 422
    [event] = AuditService(wired.uow()).list(action="project.update")
    assert event.details == {"changes": {"git_transport": "ssh"}}
    bare = _anonymous(client)
    read_only = _bearer(client, csrf, "read")
    assert (
        bare.patch("/api/projects/demo", json={"name": "x"}, headers=read_only).status_code == 403
    )


# --- branch head -----------------------------------------------------------------------------


def test_head_returns_the_commit_details_via_the_worker(
    github: FakeGithub, api: tuple[TestClient, str]
) -> None:
    client, _ = api
    response = client.get(f"{ENV_URL}/development/head", params={"branch": "development"})
    assert response.status_code == 200
    assert response.json() == {
        "branch": "development",
        "commit_sha": SHA_A,
        "message": "Fix the thing",
        "author": "Ada",
        "committed_at": None,
    }
    assert github.lookups == [("demo", "development", True)]


def test_head_of_a_branch_with_a_slash(api: tuple[TestClient, str]) -> None:
    client, _ = api
    response = client.get(f"{ENV_URL}/development/head", params={"branch": "feature/x"})
    assert response.json()["commit_sha"] == SHA_B


def test_head_of_a_missing_branch_is_404(api: tuple[TestClient, str]) -> None:
    client, _ = api
    response = client.get(f"{ENV_URL}/development/head", params={"branch": "gone"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_head_validation_and_unknown_project(
    github: FakeGithub, api: tuple[TestClient, str]
) -> None:
    client, _ = api
    assert client.get(f"{ENV_URL}/development/head").status_code == 422
    assert client.get(f"{ENV_URL}/development/head?branch=a..b").status_code == 422
    assert (
        client.get("/api/projects/none/environments/development/head?branch=x").status_code == 404
    )
    assert github.lookups == []  # the worker is not asked about things that cannot exist


# --- manual run ------------------------------------------------------------------------------


def _manual(
    client: TestClient,
    csrf: str,
    branch: str = "development",
    sha: str = SHA_A,
    env: str = "development",
) -> object:
    return client.post(
        f"{ENV_URL}/{env}/runs",
        json={"branch": branch, "commit_sha": sha},
        headers=mutation_headers(client, csrf),
    )


def test_manual_run_queues_the_confirmed_commit(
    wired: AppContainer, github: FakeGithub, api: tuple[TestClient, str]
) -> None:
    client, csrf = api
    response = _manual(client, csrf)
    assert response.status_code == 201  # type: ignore[attr-defined]
    body = response.json()  # type: ignore[attr-defined]
    assert (body["trigger"], body["status"], body["number"]) == ("manual", "queued", 1)
    assert (body["branch"], body["commit_sha"], body["project"]) == ("development", SHA_A, "demo")
    assert body["pipeline_source"] == "platform"
    assert github.lookups == [("demo", "development", False)]
    with wired.uow() as uow:
        run = uow.runs.get(body["id"])
        assert run is not None
        assert run.triggered_by is not None
        assert run.triggered_by_token_id is None
    [event] = AuditService(wired.uow()).list(action="run.create")
    assert event.actor_type.value == "user"
    assert event.details["trigger"] == "manual"


def test_manual_run_of_another_branch_in_the_production_environment(
    api: tuple[TestClient, str],
) -> None:
    client, csrf = api
    response = _manual(client, csrf, branch="feature/x", sha=SHA_B, env="production")
    assert response.status_code == 201  # type: ignore[attr-defined]
    assert response.json()["environment"] == "production"  # type: ignore[attr-defined]


def test_manual_run_with_a_moved_head_is_409_and_queues_nothing(
    wired: AppContainer, github: FakeGithub, api: tuple[TestClient, str]
) -> None:
    client, csrf = api
    github.heads["development"] = SHA_B  # somebody pushed after the user previewed SHA_A
    response = _manual(client, csrf, sha=SHA_A)
    assert response.status_code == 409  # type: ignore[attr-defined]
    error = response.json()["error"]  # type: ignore[attr-defined]
    assert error["code"] == "conflict"
    assert "moved" in error["message"]
    assert client.get("/api/runs").json()["total"] == 0
    assert AuditService(wired.uow()).list(action="run.create") == []


def test_manual_run_of_a_missing_branch_is_404(api: tuple[TestClient, str]) -> None:
    client, csrf = api
    assert _manual(client, csrf, branch="gone").status_code == 404  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "body",
    [
        {"branch": "development", "commit_sha": "abc"},
        {"branch": "development", "commit_sha": "A" * 40},
        {"branch": "a..b", "commit_sha": SHA_A},
        {"branch": "development"},
        {"commit_sha": SHA_A},
        {"branch": "development", "commit_sha": SHA_A, "pipeline": "x"},
    ],
)
def test_manual_run_validation(api: tuple[TestClient, str], body: dict[str, object]) -> None:
    client, csrf = api
    response = client.post(
        f"{ENV_URL}/development/runs", json=body, headers=mutation_headers(client, csrf)
    )
    assert response.status_code == 422


def test_manual_run_unknown_project(api: tuple[TestClient, str], github: FakeGithub) -> None:
    client, csrf = api
    response = client.post(
        "/api/projects/none/environments/development/runs",
        json={"branch": "development", "commit_sha": SHA_A},
        headers=mutation_headers(client, csrf),
    )
    assert response.status_code == 404
    assert github.lookups == []


def test_manual_run_scopes_csrf_and_token_attribution(
    wired: AppContainer, api: tuple[TestClient, str]
) -> None:
    client, csrf = api
    body = {"branch": "development", "commit_sha": SHA_A}
    url = f"{ENV_URL}/development/runs"
    assert client.post(url, json=body).status_code == 403
    assert client.post(url, json=body, headers={"Origin": ORIGIN}).status_code == 403
    bare = _anonymous(client)
    assert bare.post(url, json=body).status_code == 401
    assert bare.post(url, json=body, headers=_bearer(client, csrf, "read")).status_code == 403
    response = bare.post(url, json=body, headers=_bearer(client, csrf, "run"))
    assert response.status_code == 201
    with wired.uow() as uow:
        run = uow.runs.get(response.json()["id"])
        assert run is not None
        assert run.triggered_by is None
        assert run.triggered_by_token_id is not None
    [event] = AuditService(wired.uow()).list(action="run.create")
    assert event.actor_type.value == "token"


# --- retry -----------------------------------------------------------------------------------


def test_retry_queues_the_same_commit_and_branch_as_manual(
    wired: AppContainer, clock: FakeClock, api: tuple[TestClient, str]
) -> None:
    client, csrf = api
    original = _queue(wired, sha=SHA_B, branch="feature/x")
    _finish(wired)
    clock.advance(minutes=1)
    response = client.post(f"/api/runs/{original.id}/retry", headers=mutation_headers(client, csrf))
    assert response.status_code == 201
    body = response.json()
    assert body["id"] != original.id
    assert (body["trigger"], body["status"], body["number"]) == ("manual", "queued", 2)
    assert (body["branch"], body["commit_sha"]) == ("feature/x", SHA_B)
    assert (body["project"], body["environment"]) == ("demo", "development")
    [event] = AuditService(wired.uow()).list(action="run.create")
    assert event.details["retry_of"] == original.id


def test_retry_of_an_unfinished_run_is_409(
    wired: AppContainer, api: tuple[TestClient, str]
) -> None:
    client, csrf = api
    queued = _queue(wired)
    response = client.post(f"/api/runs/{queued.id}/retry", headers=mutation_headers(client, csrf))
    assert response.status_code == 409
    assert client.get("/api/runs").json()["total"] == 1


def test_retry_of_a_production_run_stays_in_production(
    wired: AppContainer, api: tuple[TestClient, str]
) -> None:
    client, csrf = api
    original = _queue(wired, PROD, branch="production")
    _finish(wired, RunStatus.SUCCEEDED)
    response = client.post(f"/api/runs/{original.id}/retry", headers=mutation_headers(client, csrf))
    assert response.json()["environment"] == "production"


def test_retry_unknown_run_and_permissions(
    wired: AppContainer, api: tuple[TestClient, str]
) -> None:
    client, csrf = api
    headers = mutation_headers(client, csrf)
    assert client.post("/api/runs/999/retry", headers=headers).status_code == 404
    run = _queue(wired)
    _finish(wired)
    url = f"/api/runs/{run.id}/retry"
    assert client.post(url).status_code == 403
    assert client.post(url, headers={"Origin": ORIGIN}).status_code == 403
    bare = _anonymous(client)
    assert bare.post(url).status_code == 401
    assert bare.post(url, headers=_bearer(client, csrf, "read")).status_code == 403
    assert bare.post(url, headers=_bearer(client, csrf, "run")).status_code == 201


def test_runs_created_queued_at_different_times_are_listed_newest_first(
    wired: AppContainer, clock: FakeClock, api: tuple[TestClient, str]
) -> None:
    client, _ = api
    first = _queue(wired)
    clock.advance(minutes=30)
    second = _queue(wired)
    page = client.get("/api/runs").json()
    assert [r["id"] for r in page["items"]] == [second.id, first.id]
