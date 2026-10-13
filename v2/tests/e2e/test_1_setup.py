"""Steps 1-3: admin and token, the project configuration, a manual run that deploys."""

from __future__ import annotations

import pytest

from tests.e2e.client import Api
from tests.e2e.sample import build_seed_image, create_project, create_repo
from tests.e2e.scenario import ENV, SLUG, Scenario, container_ids, image_of, web_version
from tests.e2e.stack import Stack

pytestmark = pytest.mark.e2e


def test_01_admin_login_and_token(stack: Stack, api: Api, scenario: Scenario) -> None:
    session = api.get("/api/auth/session")
    assert session["user"]["username"] == "e2e-admin"
    created = api.post(
        "/api/tokens", {"name": "e2e-agent", "scopes": ["read", "run", "admin"]}, expect=201
    )
    assert created["token"].startswith("jdt_")
    scenario.token = created["token"]
    listed = api.get("/api/tokens")
    assert [t["name"] for t in listed] == ["e2e-agent"]
    assert "token" not in listed[0]


def test_02_project_configuration(stack: Stack, api: Api, scenario: Scenario) -> None:
    seed = build_seed_image(stack)
    scenario.project = create_project(stack, seed)
    scenario.repo = create_repo(stack)
    scenario.shas["v1"] = scenario.repo.commit("1")
    assert web_version(stack) == "version seed"
    project = api.post(
        "/api/projects",
        {
            "slug": SLUG,
            "name": "Shop",
            "repository": "acme/app",
            "development_branch": "main",
            "production_branch": "production",
            "git_transport": "https",
        },
        expect=201,
    )
    assert [e["name"] for e in project["environments"]] == ["development", "production"]
    directory = scenario.sample().directory
    environment = api.patch(
        ENV,
        {
            "compose_project": stack.app_project,
            "compose_files": [str(directory / "compose.yml")],
            "compose_env_files": [str(directory / "ports.env")],
            "services": ["web", "worker", "db"],
        },
    )
    assert environment["compose_project"] == stack.app_project
    head = api.get(f"{ENV}/head?branch=main")
    assert head["commit_sha"] == scenario.shas["v1"]
    assert head["message"] == "version 1"


def test_03_manual_run_deploys_only_the_selected_service(
    stack: Stack, api: Api, scenario: Scenario
) -> None:
    sample = scenario.sample()
    before = container_ids(sample)
    head = api.get(f"{ENV}/head?branch=main")  # step one: preview the commit
    run = api.post(f"{ENV}/runs", {"branch": "main", "commit_sha": head["commit_sha"]}, expect=201)
    assert run["status"] == "queued"
    assert run["trigger"] == "manual"
    final, events = api.follow_run(run["id"])
    log = api.run_log(final)
    assert final["status"] == "succeeded", f"{final['message']}\n{log[-3000:]}"
    assert {e.name for e in events} >= {"status", "log", "end"}
    assert [s["name"] for s in final["stages"]] == ["Checkout", "Build", "Test", "Deploy", "Verify"]
    assert all(s["status"] == "succeeded" for s in final["stages"])
    assert "tests-passed" in log
    assert "deployment succeeded" in log
    page = api.get(f"{ENV}/deployments")
    assert page["total"] == 1
    deployment = page["items"][0]
    assert deployment["status"] == "succeeded"
    assert deployment["commit_sha"] == scenario.shas["v1"]
    change = deployment["services"][0]
    assert change["service"] == "web"
    assert change["previous_image"].endswith("-seed:v0")
    assert change["new_image"].startswith("sc/shop-development-web:")
    after = container_ids(sample)
    assert after["worker"] == before["worker"]
    assert after["db"] == before["db"]
    assert after["web"] != before["web"]
    assert image_of(stack, after["web"]) == change["new_image"]
    assert web_version(stack) == "version 1"
    scenario.data["image_v1"] = change["new_image"]
    scenario.data["ids_v1"] = after
