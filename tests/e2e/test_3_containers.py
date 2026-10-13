"""Step 5: container inventory, two-step restart, environment edit and logs."""

from __future__ import annotations

from typing import Any

import pytest

from tests.e2e.client import Api
from tests.e2e.scenario import ENV, Scenario, web_version
from tests.e2e.stack import Stack

pytestmark = pytest.mark.e2e
CONTAINERS = f"{ENV}/containers"


def _web(api: Api) -> dict[str, Any]:
    inventory = api.get(CONTAINERS)
    web = [c for c in inventory if c["service"] == "web"]
    assert len(web) == 1, inventory
    assert isinstance(web[0], dict)
    return web[0]


def test_07_inventory_lists_the_environment_containers(
    stack: Stack, api: Api, scenario: Scenario
) -> None:
    inventory = api.get(CONTAINERS)
    assert [c["service"] for c in inventory] == ["db", "web", "worker"]
    web = next(c for c in inventory if c["service"] == "web")
    assert web["image"] == scenario.data["image_v2"]
    assert web["state"] == "running"
    assert web["health"] == "healthy"
    assert web["commit"] == scenario.shas["v2"]
    assert len(web["id"]) == 64
    assert any(p["container_port"].startswith("8080") for p in web["ports"]), web
    db = next(c for c in inventory if c["service"] == "db")
    assert any(m["destination"] == "/docker-entrypoint-initdb.d/init.sql" for m in db["mounts"])
    detail = api.get(f"{CONTAINERS}/{web['id']}")
    assert detail["id"] == web["id"]
    # another project's containers are not visible: the platform's own api container is a 404
    own = stack.container_id("api")
    api.get(f"{CONTAINERS}/{own}", expect=404)
    scenario.data["web_id"] = web["id"]


def test_08_restart_needs_a_confirmation(stack: Stack, api: Api, scenario: Scenario) -> None:
    web = _web(api)
    base = f"{CONTAINERS}/{web['id']}/actions"
    step_one = api.post(base, {"action": "restart"})
    assert step_one["action"] == "restart"
    assert step_one["effects"]
    # step one changed nothing
    assert _web(api)["started_at"] == web["started_at"]
    api.post(f"{base}/confirm", {"confirmation": "not-the-confirmation"}, expect=422)
    done = api.post(f"{base}/confirm", {"confirmation": step_one["confirmation"]})
    assert done["previous_state"] == "running"
    # a confirmation is single use
    api.post(f"{base}/confirm", {"confirmation": step_one["confirmation"]}, expect=(404, 409, 422))
    restarted = _web(api)
    assert restarted["started_at"] != web["started_at"]
    assert restarted["image"] == scenario.data["image_v2"]
    assert web_version(stack) == "version 2"


def test_09_environment_edit_recreates_only_that_service_and_keeps_the_image(
    stack: Stack, api: Api, scenario: Scenario
) -> None:
    before = {c["service"]: c for c in api.get(CONTAINERS)}
    web = before["web"]
    result = api.put(f"{CONTAINERS}/{web['id']}/env", {"set": {"E2E_FLAG": "on"}, "remove": []})
    assert result["service"] == "web"
    assert result["restarted"] is True
    after = {c["service"]: c for c in api.get(CONTAINERS)}
    assert after["web"]["id"] != web["id"]  # recreated
    assert after["worker"]["id"] == before["worker"]["id"]  # nothing else was touched
    assert after["db"]["id"] == before["db"]["id"]
    assert after["web"]["image"] == scenario.data["image_v2"]  # the deployed image is kept
    assert after["web"]["state"] == "running"
    names = {e["name"]: e for e in api.get(f"{CONTAINERS}/{after['web']['id']}/env")}
    assert names["E2E_FLAG"]["overridden"] is True
    assert names["E2E_FLAG"]["masked_value"] == "********"
    assert web_version(stack) == "version 2"
    scenario.data["web_id"] = after["web"]["id"]


def test_10_logs_tail_shows_the_startup_line(stack: Stack, api: Api, scenario: Scenario) -> None:
    web_id = scenario.data["web_id"]
    logs = api.get(f"{CONTAINERS}/{web_id}/logs?tail=50")
    texts = [line["text"] for line in logs["lines"]]
    assert any("web version 2 starting" in text for text in texts), texts
    assert logs["dropped"] == 0
    api.get(f"{CONTAINERS}/{web_id}/logs?tail=5000", expect=422)
