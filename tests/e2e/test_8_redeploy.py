"""Step 10 (beyond the brief): redeploy an earlier version, and the worker leaves nothing behind."""

from __future__ import annotations

import pytest

from tests.e2e.client import Api
from tests.e2e.scenario import ENV, Scenario, container_ids, image_of, web_version
from tests.e2e.stack import Stack

pytestmark = pytest.mark.e2e


def test_18_redeploy_the_first_version_without_rebuilding(
    stack: Stack, api: Api, scenario: Scenario
) -> None:
    page = api.get(f"{ENV}/deployments")
    first = next(d for d in page["items"] if d["commit_sha"] == scenario.shas["v1"])
    assert first["status"] == "succeeded"
    queued = api.post(f"{ENV}/deployments/{first['id']}/redeploy", expect=201)
    assert queued["run"]["trigger"] == "redeploy"
    final, _events = api.follow_run(queued["run"]["id"])
    log = api.run_log(final)
    assert final["status"] == "succeeded", f"{final['message']}\n{log[-2500:]}"
    stages = {s["name"]: s["status"] for s in final["stages"]}
    assert stages == {"Checkout": "skipped", "Redeploy": "succeeded"}
    assert web_version(stack) == "version 1"
    assert image_of(stack, container_ids(scenario.sample())["web"]) == scenario.data["image_v1"]
    newest = api.get(f"{ENV}/deployments")["items"][0]
    assert newest["status"] == "succeeded"
    assert newest["redeploy_of"] == first["id"]
    assert newest["id"] == queued["deployment_id"]


def test_19_the_worker_leaves_no_workspaces_behind(
    stack: Stack, api: Api, scenario: Scenario
) -> None:
    listing = stack.exec_worker("ls", "-A", "/var/lib/jendock/workspaces").stdout
    assert listing.strip() == ""
    runs = api.get(f"{ENV}/runs?limit=50")
    assert runs["total"] == 4
    assert [r["status"] for r in runs["items"]] == ["succeeded", "failed", "succeeded", "succeeded"]
    assert [r["trigger"] for r in runs["items"]] == ["redeploy", "push", "push", "manual"]
