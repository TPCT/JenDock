"""Step 4: signed webhook pushes deploy a new commit, and a failing one is rolled back."""

from __future__ import annotations

import httpx
import pytest

from tests.e2e.client import Api, send_push, sign_webhook
from tests.e2e.scenario import ENV, Scenario, container_ids, image_of, web_version
from tests.e2e.stack import Stack

pytestmark = pytest.mark.e2e
REPOSITORY = "acme/app"


def _push(stack: Stack, api: Api, sha: str, **kwargs: str) -> httpx.Response:
    return send_push(
        api, stack.webhook_secret, repository=REPOSITORY, branch="main", after=sha, **kwargs
    )


def test_04_webhook_rejects_bad_signatures_and_unknown_repositories(
    stack: Stack, api: Api, scenario: Scenario
) -> None:
    forged = httpx.post(
        f"{api.base_url}/api/github/webhook",
        content=b"{}",
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "forged-1",
            "X-Hub-Signature-256": sign_webhook("not-the-secret", b"{}"),
        },
        timeout=30.0,
    )
    assert forged.status_code == 401
    other = send_push(
        api, stack.webhook_secret, repository="acme/unknown", branch="main", after="a" * 40
    )
    assert other.status_code == 202
    assert other.json()["outcome"] == "ignored"
    # auto-deploy is off: the delivery is recorded and no run is queued
    recorded = _push(stack, api, scenario.shas["v1"])
    assert recorded.json() == {"outcome": "recorded", "runs": [], "reason": "auto-deploy is off"}


def test_05_push_to_the_tracked_branch_deploys_the_new_commit(
    stack: Stack, api: Api, scenario: Scenario
) -> None:
    api.patch(ENV, {"auto_deploy": True})
    sample = scenario.sample()
    before = container_ids(sample)
    sha = scenario.source().commit("2")
    scenario.shas["v2"] = sha
    delivered = _push(stack, api, sha, before=scenario.shas["v1"])
    assert delivered.status_code == 202, delivered.text
    body = delivered.json()
    assert body["outcome"] == "queued"
    assert len(body["runs"]) == 1
    final, _events = api.follow_run(body["runs"][0])
    assert final["status"] == "succeeded", f"{final['message']}\n{api.run_log(final)[-2500:]}"
    assert final["trigger"] == "push"
    assert final["commit_sha"] == sha
    assert web_version(stack) == "version 2"
    page = api.get(f"{ENV}/deployments")
    assert [d["status"] for d in page["items"]] == ["succeeded", "succeeded"]
    latest = page["items"][0]
    assert latest["commit_sha"] == sha
    assert latest["services"][0]["previous_image"] == scenario.data["image_v1"]
    new_image = latest["services"][0]["new_image"]
    assert new_image.startswith(f"sc/shop-development-web:{sha[:12]}")
    after = container_ids(sample)
    assert image_of(stack, after["web"]) == new_image
    assert after["worker"] == before["worker"]
    assert after["db"] == before["db"]
    # the first deployment's image is still there to roll back or redeploy to
    assert stack.docker("image", "inspect", scenario.data["image_v1"]).returncode == 0
    scenario.data["image_v2"] = new_image


def test_06_a_failing_health_check_rolls_back_automatically(
    stack: Stack, api: Api, scenario: Scenario
) -> None:
    sample = scenario.sample()
    bad = scenario.source().commit("3", healthy=False)  # no index.html: GET / answers 404
    scenario.shas["v3"] = bad
    delivered = _push(stack, api, bad, before=scenario.shas["v2"])
    assert delivered.status_code == 202, delivered.text
    final, _events = api.follow_run(delivered.json()["runs"][0])
    log = api.run_log(final)
    assert final["status"] == "failed", f"{final['status']}\n{log[-2500:]}"
    assert "rolled back to the previous images" in (final["message"] or "")
    assert "health check failed" in (final["message"] or "") + log
    deploy = next(s for s in final["stages"] if s["name"] == "Deploy")
    assert deploy["status"] == "failed"
    verify = next(s for s in final["stages"] if s["name"] == "Verify")
    assert verify["status"] == "skipped"
    assert web_version(stack) == "version 2"
    assert image_of(stack, container_ids(sample)["web"]) == scenario.data["image_v2"]
    newest = api.get(f"{ENV}/deployments")["items"][0]
    assert newest["status"] == "rolled_back"
    assert newest["commit_sha"] == bad
    assert "rolled back" in newest["message"]
    assert api.get(f"{ENV}/deployments")["total"] == 3
