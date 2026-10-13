"""Pipeline endpoints: stored pipeline, versions, concurrency, editor helpers, scopes, CSRF."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, replace

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from backend.adapters.worker_client import RepositoryFile
from backend.api.container import AppContainer
from backend.domain.actor import Actor
from backend.domain.enums import ActorType, EnvironmentName
from backend.domain.errors import WorkerUnavailableError
from backend.pipeline.templates import load_templates
from backend.repositories.database import UnitOfWork
from backend.repositories.models import Secret
from backend.services.audit import AuditService
from backend.services.lookup import require_environment
from tests.api_support import login, make_client, mutation_headers
from tests.conftest import DEV, PROD
from tests.pipeline_support import FIXTURES, wrap

BASE = "/api/projects/demo/environments/development/pipeline"
LARAVEL = (FIXTURES / "valid" / "laravel.groovy").read_text()
SIMPLE = wrap("echo 'hi'", "agent any")
SYNTAX_ERROR = "pipeline {"


@dataclass
class FakeRepository:
    """Stands in for the worker: the Jenkinsfile per environment, or a failure."""

    files: dict[EnvironmentName, str]
    reachable: bool = True
    down: bool = False

    def repository_jenkinsfile(
        self, slug: str, env: EnvironmentName, actor: Actor
    ) -> RepositoryFile:
        if self.down:
            raise WorkerUnavailableError("worker is not reachable")
        return RepositoryFile(self.reachable, self.files.get(env))


@pytest.fixture
def repo() -> FakeRepository:
    return FakeRepository({})


@pytest.fixture
def session(
    container: AppContainer, repo: FakeRepository, admin: int, project: str
) -> tuple[TestClient, str]:
    client = make_client(replace(container, repository=repo))
    return client, login(client)


def _put(client: TestClient, csrf: str, text: str, base: int, path: str = BASE) -> Response:
    body = {"jenkinsfile": text, "base_version": base}
    response: Response = client.put(path, json=body, headers=mutation_headers(client, csrf))
    return response


def _post(client: TestClient, csrf: str, path: str, body: Mapping[str, object]) -> Response:
    response: Response = client.post(path, json=body, headers=mutation_headers(client, csrf))
    return response


def _token(client: TestClient, csrf: str, scopes: list[str]) -> dict[str, str]:
    body = {"name": "agent", "scopes": scopes}
    created = client.post("/api/tokens", json=body, headers=mutation_headers(client, csrf))
    return {"Authorization": f"Bearer {created.json()['token']}"}


# --- the stored pipeline -----------------------------------------------------------------------


def test_nothing_stored_means_source_none(session: tuple[TestClient, str]) -> None:
    client, _ = session
    response = client.get(BASE)
    assert response.status_code == 200
    assert response.json() == {
        "source": "none",
        "version": 0,
        "jenkinsfile": "",
        "updated_at": None,
        "updated_by": None,
    }


def test_save_then_read_back_with_author(session: tuple[TestClient, str]) -> None:
    client, csrf = session
    saved = _put(client, csrf, SIMPLE, 0)
    assert saved.status_code == 200
    body = saved.json()
    assert (body["source"], body["version"], body["jenkinsfile"]) == ("platform", 1, SIMPLE)
    assert body["updated_by"] == "admin"
    assert body["updated_at"].startswith("2026-10-13T12:00:00")
    assert client.get(BASE).json() == body
    second = _put(client, csrf, LARAVEL.replace("DB_PASSWORD", "DB_PASSWORD"), 1)
    assert second.status_code == 422  # unknown credential


def test_stale_base_version_is_a_conflict_and_stores_nothing(
    session: tuple[TestClient, str],
) -> None:
    client, csrf = session
    assert _put(client, csrf, SIMPLE, 0).status_code == 200
    stale = _put(client, csrf, SIMPLE.replace("hi", "other"), 0)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "conflict"
    ahead = _put(client, csrf, SIMPLE, 5)
    assert ahead.status_code == 409
    assert [v["version"] for v in client.get(f"{BASE}/versions").json()] == [1]
    assert _put(client, csrf, SIMPLE.replace("hi", "v2"), 1).status_code == 200


def test_invalid_pipeline_is_422_with_positioned_diagnostics(
    session: tuple[TestClient, str],
) -> None:
    client, csrf = session
    response = _put(client, csrf, wrap("foo 'y'", "agent any"), 0)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["diagnostics"][0]["line"] == 7
    assert "unknown step 'foo'" in error["diagnostics"][0]["message"]
    syntax = _put(client, csrf, SYNTAX_ERROR, 0).json()["error"]
    assert len(syntax["diagnostics"]) == 1
    assert client.get(BASE).json()["version"] == 0


def test_secret_names_of_the_environment_are_used_when_saving(
    session: tuple[TestClient, str], uow: UnitOfWork
) -> None:
    client, csrf = session
    with uow:
        environment = require_environment(uow, "demo", DEV)
        uow.secrets.add(Secret(environment_id=environment.id, name="DB_PASSWORD", ciphertext=b"x"))
    assert _put(client, csrf, LARAVEL, 0).status_code == 200


def test_the_repository_file_wins_and_blocks_saving(
    session: tuple[TestClient, str], repo: FakeRepository
) -> None:
    client, csrf = session
    assert _put(client, csrf, SIMPLE, 0).status_code == 200
    repo.files[DEV] = "// from git\npipeline { }\n"
    body = client.get(BASE).json()
    assert body == {
        "source": "repository",
        "version": 1,  # the latest platform version stays visible
        "jenkinsfile": "// from git\npipeline { }\n",
        "updated_at": None,
        "updated_by": None,
    }
    blocked = _put(client, csrf, SIMPLE.replace("hi", "x"), 1)
    assert blocked.status_code == 409
    assert "repository" in blocked.json()["error"]["message"]
    assert [v["version"] for v in client.get(f"{BASE}/versions").json()] == [1]
    other = client.get("/api/projects/demo/environments/production/pipeline").json()
    assert other["source"] == "none"  # the other environment has its own branch


def test_an_unreadable_repository_falls_back_to_the_stored_pipeline(
    session: tuple[TestClient, str], repo: FakeRepository
) -> None:
    client, csrf = session
    _put(client, csrf, SIMPLE, 0)
    repo.reachable = False
    assert client.get(BASE).json()["source"] == "platform"
    assert _put(client, csrf, SIMPLE.replace("hi", "v2"), 1).status_code == 200


def test_a_missing_worker_is_a_503_not_a_wrong_answer(
    session: tuple[TestClient, str], repo: FakeRepository
) -> None:
    client, csrf = session
    repo.down = True
    assert client.get(BASE).status_code == 503
    assert _put(client, csrf, SIMPLE, 0).status_code == 503


def test_unknown_project_or_environment_is_404(session: tuple[TestClient, str]) -> None:
    client, csrf = session
    assert client.get("/api/projects/nope/environments/development/pipeline").status_code == 404
    assert client.get("/api/projects/demo/environments/staging/pipeline").status_code == 422
    missing = _put(client, csrf, SIMPLE, 0, "/api/projects/nope/environments/development/pipeline")
    assert missing.status_code == 404


def test_versions_list_and_detail(session: tuple[TestClient, str]) -> None:
    client, csrf = session
    _put(client, csrf, SIMPLE, 0)
    _put(client, csrf, SIMPLE.replace("hi", "two"), 1)
    listed = client.get(f"{BASE}/versions").json()
    assert [v["version"] for v in listed] == [2, 1]
    assert listed[0]["created_by"] == "admin"
    assert "jenkinsfile" not in listed[0]
    one = client.get(f"{BASE}/versions/1").json()
    assert one["jenkinsfile"] == SIMPLE
    assert one["created_by"] == "admin"
    assert client.get(f"{BASE}/versions/9").status_code == 404
    assert client.get(f"{BASE}/versions/abc").status_code == 422


def test_saving_is_audited_without_the_pipeline_text(
    session: tuple[TestClient, str], container: AppContainer
) -> None:
    client, csrf = session
    _put(client, csrf, SIMPLE, 0)
    events = AuditService(container.uow()).list(action="pipeline.save")
    assert len(events) == 1
    assert events[0].details == {"version": 1, "bytes": len(SIMPLE.encode())}
    assert events[0].actor_type is ActorType.USER


# --- scopes and CSRF ---------------------------------------------------------------------------


def test_all_endpoints_need_authentication(session: tuple[TestClient, str]) -> None:
    client, _ = session
    bare = TestClient(client.app, base_url=str(client.base_url))
    for path in (
        BASE,
        f"{BASE}/versions",
        f"{BASE}/versions/1",
        "/api/pipelines/catalog",
        "/api/pipelines/templates",
    ):
        assert bare.get(path).status_code == 401, path
    for path in ("/api/pipelines/parse", "/api/pipelines/render", "/api/pipelines/validate"):
        assert bare.post(path, json={}).status_code == 401, path
    assert bare.put(BASE, json={}).status_code == 401
    assert bare.post(f"{BASE}/dry-run", json={}).status_code == 401


def test_saving_needs_the_pipelines_scope(session: tuple[TestClient, str]) -> None:
    client, csrf = session
    bare = TestClient(client.app, base_url=str(client.base_url))
    body = {"jenkinsfile": SIMPLE, "base_version": 0}
    reader = _token(client, csrf, ["read"])
    assert bare.get(BASE, headers=reader).status_code == 200
    assert bare.put(BASE, json=body, headers=reader).status_code == 403
    writer = _token(client, csrf, ["pipelines"])
    assert bare.get(BASE, headers=writer).status_code == 403  # no read scope
    both = _token(client, csrf, ["read", "pipelines"])
    assert bare.put(BASE, json=body, headers=both).status_code == 200
    admin_token = _token(client, csrf, ["admin"])
    assert bare.get(BASE, headers=admin_token).json()["updated_by"] is None  # tokens have no user
    assert (
        bare.post("/api/pipelines/parse", json={"jenkinsfile": SIMPLE}, headers=reader).status_code
        == 200
    )


def test_mutations_by_session_need_csrf_and_origin(session: tuple[TestClient, str]) -> None:
    client, csrf = session
    body = {"jenkinsfile": SIMPLE, "base_version": 0}
    assert client.put(BASE, json=body).status_code == 403
    wrong = {"Origin": "https://evil.example", "X-CSRF-Token": csrf}
    assert client.put(BASE, json=body, headers=wrong).status_code == 403
    no_token = {"Origin": str(client.base_url).rstrip("/")}
    assert client.put(BASE, json=body, headers=no_token).status_code == 403
    for path in ("/api/pipelines/parse", "/api/pipelines/render", "/api/pipelines/validate"):
        assert client.post(path, json={"jenkinsfile": SIMPLE}).status_code == 403, path
    assert client.post(f"{BASE}/dry-run", json={}).status_code == 403
    assert client.get(BASE).json()["version"] == 0


def test_request_bodies_are_strict(session: tuple[TestClient, str]) -> None:
    client, csrf = session
    for body in (
        {"jenkinsfile": SIMPLE},
        {"jenkinsfile": SIMPLE, "base_version": -1},
        {"jenkinsfile": SIMPLE, "base_version": 0, "extra": 1},
        {"jenkinsfile": 5, "base_version": 0},
    ):
        response = client.put(BASE, json=body, headers=mutation_headers(client, csrf))
        assert response.status_code == 422, body
        assert response.json()["error"]["fields"]


# --- parse, render, validate -------------------------------------------------------------------


def test_parse_returns_model_and_diagnostics(session: tuple[TestClient, str]) -> None:
    client, csrf = session
    response = _post(client, csrf, "/api/pipelines/parse", {"jenkinsfile": LARAVEL})
    assert response.status_code == 200
    body = response.json()
    assert body["model"]["agent"] == {"type": "none"}
    assert body["model"]["stages"][0]["name"] == "Build image"
    # no project given: credentials() names are not judged
    assert body["diagnostics"] == []


def test_parse_syntax_error_has_null_model_and_one_diagnostic(
    session: tuple[TestClient, str],
) -> None:
    client, csrf = session
    body = _post(client, csrf, "/api/pipelines/parse", {"jenkinsfile": SYNTAX_ERROR}).json()
    assert body["model"] is None
    assert len(body["diagnostics"]) == 1
    assert set(body["diagnostics"][0]) == {"severity", "message", "line", "column"}


def test_parse_semantic_error_keeps_the_model(session: tuple[TestClient, str]) -> None:
    client, csrf = session
    text = wrap("foo 'y'", "agent any")
    body = _post(client, csrf, "/api/pipelines/parse", {"jenkinsfile": text}).json()
    assert body["model"] is not None
    assert body["model"]["stages"][0]["body"]["steps"] == [{"name": "foo", "args": {"$0": "y"}}]
    assert body["diagnostics"][0]["severity"] == "error"


def test_project_context_judges_credentials(session: tuple[TestClient, str]) -> None:
    client, csrf = session
    context = {"project": "demo", "environment": "development"}
    body = _post(client, csrf, "/api/pipelines/parse", {"jenkinsfile": LARAVEL, **context})
    messages = [d["message"] for d in body.json()["diagnostics"]]
    assert any("unknown credential 'DB_PASSWORD'" in m for m in messages)
    validated = _post(client, csrf, "/api/pipelines/validate", {"jenkinsfile": LARAVEL, **context})
    assert validated.json()["diagnostics"] == body.json()["diagnostics"]
    only_one = _post(
        client, csrf, "/api/pipelines/validate", {"jenkinsfile": LARAVEL, "project": "demo"}
    )
    assert only_one.status_code == 422
    unknown = {"jenkinsfile": LARAVEL, "project": "nope", "environment": "production"}
    assert _post(client, csrf, "/api/pipelines/validate", unknown).status_code == 404


def test_validate_reports_syntax_error_as_one_diagnostic(session: tuple[TestClient, str]) -> None:
    client, csrf = session
    body = _post(client, csrf, "/api/pipelines/validate", {"jenkinsfile": SYNTAX_ERROR}).json()
    assert len(body["diagnostics"]) == 1
    assert body["diagnostics"][0]["severity"] == "error"


def test_render_inverts_parse(session: tuple[TestClient, str]) -> None:
    client, csrf = session
    parsed = _post(client, csrf, "/api/pipelines/parse", {"jenkinsfile": LARAVEL}).json()
    rendered = _post(client, csrf, "/api/pipelines/render", {"model": parsed["model"]}).json()
    assert rendered["diagnostics"] == []
    again = _post(
        client, csrf, "/api/pipelines/parse", {"jenkinsfile": rendered["jenkinsfile"]}
    ).json()
    assert again["model"] == parsed["model"]
    canonical = _post(client, csrf, "/api/pipelines/render", {"model": again["model"]}).json()
    assert canonical["jenkinsfile"] == rendered["jenkinsfile"]


def test_render_half_filled_model_gives_text_and_diagnostics(
    session: tuple[TestClient, str],
) -> None:
    client, csrf = session
    model = {
        "agent": {"type": "any"},
        "stages": [
            {"name": "", "body": {"type": "steps", "steps": [{"name": "echo", "args": {}}]}}
        ],
    }
    response = _post(client, csrf, "/api/pipelines/render", {"model": model})
    assert response.status_code == 200
    body = response.json()
    assert body["jenkinsfile"].startswith("pipeline {")
    messages = [d["message"] for d in body["diagnostics"]]
    assert any("stage name must not be empty" in m for m in messages)
    assert any("requires argument 'message'" in m for m in messages)


def test_render_rejects_a_model_of_the_wrong_shape_with_fields(
    session: tuple[TestClient, str],
) -> None:
    client, csrf = session
    response = _post(client, csrf, "/api/pipelines/render", {"model": {"stages": "x"}})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["fields"][0]["loc"][:2] == ["body", "model"]


def test_oversized_text_is_rejected(session: tuple[TestClient, str]) -> None:
    client, csrf = session
    huge = "// " + "x" * (300 * 1024)
    assert _post(client, csrf, "/api/pipelines/parse", {"jenkinsfile": huge}).status_code == 422


# --- catalogue and templates -------------------------------------------------------------------


def test_catalog_endpoint_matches_the_contract(session: tuple[TestClient, str]) -> None:
    client, _ = session
    body = client.get("/api/pipelines/catalog").json()
    assert set(body) == {"steps", "options", "parameters", "post_conditions"}
    steps = {step["name"]: step for step in body["steps"]}
    assert set(steps) >= {"sh", "echo", "dockerBuild", "deploy", "healthCheck", "timeout"}
    sh = steps["sh"]
    assert set(sh) == {"name", "title", "description", "takes_block", "needs_container", "args"}
    assert sh["needs_container"] is True
    script = sh["args"][0]
    assert set(script) == {
        *("name", "type", "required", "default", "enum", "min", "max"),
        *("description", "multiline", "role"),
    }
    assert (script["type"], script["required"], script["multiline"]) == ("string", True, True)
    unit = next(a for a in steps["timeout"]["args"] if a["name"] == "unit")
    assert unit["enum"] == ["SECONDS", "MINUTES", "HOURS"]
    assert steps["dockerBuild"]["args"][0]["role"] == "produces_image"
    assert body["post_conditions"][0]["name"] == "always"
    assert all(step["title"] and step["description"] for step in body["steps"] + body["options"])


def test_templates_endpoint_lists_valid_starters(session: tuple[TestClient, str]) -> None:
    client, csrf = session
    listed = client.get("/api/pipelines/templates").json()
    assert [t["name"] for t in listed] == [t.name for t in load_templates()]
    for item in listed:
        assert set(item) == {"name", "title", "description", "jenkinsfile"}
        checked = _post(
            client, csrf, "/api/pipelines/validate", {"jenkinsfile": item["jenkinsfile"]}
        )
        assert checked.json()["diagnostics"] == [], item["name"]


# --- dry run -----------------------------------------------------------------------------------

DRY = """
pipeline {
    agent { docker 'node:22' }
    stages {
        stage('Test') { steps { sh 'npm test' } }
        stage('Release') {
            when { branch 'production' }
            steps { echo 'ship' }
        }
    }
}
"""


def test_dry_run_of_a_given_text_for_a_branch(session: tuple[TestClient, str]) -> None:
    client, csrf = session
    path = f"{BASE}/dry-run"
    body = _post(client, csrf, path, {"jenkinsfile": DRY, "branch": "production"}).json()
    assert body["valid"] is True
    plan = body["plan"]
    assert (plan["branch"], plan["environment"], plan["agent"]) == (
        "production",
        "development",
        "docker node:22",
    )
    assert [(s["name"], s["will_run"]) for s in plan["stages"]] == [
        ("Test", True),
        ("Release", True),
    ]
    assert plan["stages"][0]["steps"] == [{"name": "sh", "label": "sh: npm test", "children": []}]
    other = _post(client, csrf, path, {"jenkinsfile": DRY, "branch": "main"}).json()
    release = other["plan"]["stages"][1]
    assert release["will_run"] is False
    assert "does not match branch 'main'" in release["skip_reason"]


def test_dry_run_defaults_to_the_environment_branch_and_the_pipeline_in_force(
    session: tuple[TestClient, str],
) -> None:
    client, csrf = session
    _put(client, csrf, DRY, 0)
    body = _post(client, csrf, f"{BASE}/dry-run", {}).json()
    assert body["plan"]["branch"] == "development"  # the environment's own branch
    assert body["plan"]["stages"][1]["will_run"] is False


def test_dry_run_of_an_invalid_pipeline_returns_diagnostics_and_no_plan(
    session: tuple[TestClient, str],
) -> None:
    client, csrf = session
    body = _post(client, csrf, f"{BASE}/dry-run", {"jenkinsfile": wrap("foo 'x'")}).json()
    assert body["valid"] is False
    assert body["plan"] is None
    assert body["diagnostics"]
    syntax = _post(client, csrf, f"{BASE}/dry-run", {"jenkinsfile": SYNTAX_ERROR}).json()
    assert (syntax["valid"], syntax["plan"]) == (False, None)


def test_dry_run_without_any_pipeline_is_422(session: tuple[TestClient, str]) -> None:
    client, csrf = session
    response = _post(client, csrf, f"{BASE}/dry-run", {})
    assert response.status_code == 422


def test_dry_run_uses_the_repository_file_when_there_is_one(
    session: tuple[TestClient, str], repo: FakeRepository
) -> None:
    client, csrf = session
    repo.files[PROD] = DRY
    path = "/api/projects/demo/environments/production/pipeline/dry-run"
    plan = _post(client, csrf, path, {}).json()["plan"]
    assert plan["branch"] == "production"
    assert plan["stages"][1]["will_run"] is True


def test_racing_saves_with_the_same_base_version_cannot_both_win(
    session: tuple[TestClient, str],
) -> None:
    client, csrf = session
    outcomes: list[int] = []
    barrier = threading.Barrier(6)

    def save(index: int) -> None:
        barrier.wait()
        outcomes.append(_put(client, csrf, SIMPLE.replace("hi", f"r{index}"), 0).status_code)

    threads = [threading.Thread(target=save, args=(index,)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == [200, 409, 409, 409, 409, 409]
    assert [v["version"] for v in client.get(f"{BASE}/versions").json()] == [1]
