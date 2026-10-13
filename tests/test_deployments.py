"""Deployment records, history API, redeploy and the redeploy run."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from backend.adapters.container_runtime import CancelSignal, LineSink
from backend.adapters.deploy_ledger import ServiceChange
from backend.adapters.deployer import DeployRequest
from backend.adapters.errors import DeployError
from backend.api.container import AppContainer
from backend.domain.enums import DeploymentStatus, RunStatus, RunTrigger
from backend.domain.paging import PageQuery
from backend.repositories.models import Run
from backend.services.audit import AuditService
from backend.services.deployments import DatabaseLedger, DeploymentService, DeploymentView
from tests.api_support import FakeClock, login, make_client, mutation_headers
from tests.conftest import DEV, PROD
from tests.execution_support import Harness
from tests.test_pipeline_runner import pipeline, stage

HISTORY = "/api/projects/demo/environments/development/deployments"
WEB_TAG = "jendock/demo-development-web:0123456789ab-r1"
OLD_TAG = "app-web:old"


class LedgerDeployer:
    """A deployer that behaves like ``ComposeDeployer`` towards the ledger only."""

    def __init__(self, ledger: DatabaseLedger, fail: str | None = None) -> None:
        self.ledger = ledger
        self.fail = fail
        self.requests: list[DeployRequest] = []

    def deploy(self, request: DeployRequest, sink: LineSink, cancel: CancelSignal) -> None:
        self.requests.append(request)
        deployment_id = self.ledger.begin(
            request.project, request.environment, request.run_id, request.commit_sha
        )
        changes = [
            ServiceChange(s.service, s.image, f"sha256:{s.service}", OLD_TAG, "sha256:old")
            for s in request.services
        ]
        self.ledger.record_services(deployment_id, changes)
        if self.fail:
            self.ledger.finish(deployment_id, DeploymentStatus.ROLLED_BACK, self.fail)
            raise DeployError(self.fail)
        self.ledger.finish(deployment_id, DeploymentStatus.SUCCEEDED, None)


@pytest.fixture
def ledger(harness: Harness) -> DatabaseLedger:
    return DatabaseLedger(harness.env.uow)


@pytest.fixture
def deployed(harness: Harness, ledger: DatabaseLedger) -> Run:
    """A finished run that built and deployed ``web``; the ledger holds its deployment."""
    harness.env = replace(harness.env, deployer=LedgerDeployer(ledger))
    steps = "dockerBuild(image: 'web')\ndeploy(service: 'web', image: 'web')"
    run, outcome = harness.run(pipeline(stage("Ship", steps), agent="agent none"))
    assert outcome.status is RunStatus.SUCCEEDED, outcome.message
    return run


@pytest.fixture
def session(container: AppContainer, admin: int, project: str) -> tuple[TestClient, str]:
    client = make_client(container)
    return client, login(client)


def _page(client: TestClient, query: str = "") -> Any:
    response = client.get(f"{HISTORY}{query}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


# --- ledger ----------------------------------------------------------------------------------


def test_ledger_records_a_deployment(deployed: Run, harness: Harness) -> None:
    page = _page_via_service(harness)
    assert len(page) == 1
    view = page[0]
    assert view.status is DeploymentStatus.SUCCEEDED
    assert view.run_id == deployed.id
    assert view.run_number == 1
    assert view.services[0].previous_image == OLD_TAG
    assert view.services[0].new_image == WEB_TAG
    assert view.started_at is not None
    assert view.finished_at is not None


def _page_via_service(harness: Harness) -> list[DeploymentView]:
    page = DeploymentService(harness.uow()).history("demo", DEV, PageQuery(per_page=50))
    return list(page.items)


def test_fail_unfinished_marks_only_deploying_rows(ledger: DatabaseLedger, deployed: Run) -> None:
    open_id = ledger.begin("demo", "development", deployed.id, "a" * 40)
    assert ledger.fail_unfinished("interrupted") == [open_id]
    assert ledger.fail_unfinished("interrupted") == []


def test_protected_images_cover_the_last_deployment(ledger: DatabaseLedger, deployed: Run) -> None:
    protected = ledger.protected_images("demo", "development")
    assert {WEB_TAG, OLD_TAG, "sha256:old", "sha256:web"} <= protected
    assert ledger.protected_images("demo", "production") == frozenset()


# --- history API -----------------------------------------------------------------------------


def test_history_lists_newest_first_with_paging(
    session: tuple[TestClient, str], deployed: Run, ledger: DatabaseLedger
) -> None:
    client, _ = session
    second = ledger.begin("demo", "development", deployed.id, "c" * 40)
    ledger.finish(second, DeploymentStatus.ROLLED_BACK, "rolled back")
    body = _page(client)
    assert body["total"] == 2
    items = body["items"]
    assert isinstance(items, list)
    assert [i["status"] for i in items] == ["rolled_back", "succeeded"]
    assert items[1]["services"][0] == {
        "service": "web",
        "previous_image": OLD_TAG,
        "previous_image_id": "sha256:old",
        "new_image": WEB_TAG,
        "new_image_id": "sha256:web",
    }
    assert items[1]["redeploy_of"] is None
    paged = _page(client, "?limit=1&offset=1")
    assert [i["status"] for i in paged["items"]] == ["succeeded"]
    assert (paged["limit"], paged["offset"]) == (1, 1)


def test_history_validation_and_not_found(session: tuple[TestClient, str]) -> None:
    client, _ = session
    assert client.get(f"{HISTORY}?limit=0").status_code == 422
    assert client.get(f"{HISTORY}?limit=101").status_code == 422
    assert client.get("/api/projects/nope/environments/development/deployments").status_code == 404
    assert client.get("/api/projects/demo/environments/staging/deployments").status_code == 422
    assert _page(client, "")["items"] == []


def test_history_needs_authentication(container: AppContainer) -> None:
    assert make_client(container).get(HISTORY).status_code == 401


# --- redeploy --------------------------------------------------------------------------------


def _redeploy(client: TestClient, csrf: str, deployment_id: int) -> Any:
    return client.post(
        f"{HISTORY}/{deployment_id}/redeploy", headers=mutation_headers(client, csrf)
    )


def test_redeploy_queues_a_run_that_reuses_the_images(
    session: tuple[TestClient, str], deployed: Run, harness: Harness
) -> None:
    client, csrf = session
    source = _page(client)["items"][0]["id"]
    response = _redeploy(client, csrf, source)
    assert response.status_code == 201, response.text
    body = response.json()
    run = body["run"]
    assert run["trigger"] == "redeploy"
    assert run["status"] == "queued"
    assert run["pipeline_source"] == "platform"
    assert run["number"] == 2
    assert (run["project"], run["environment"]) == ("demo", "development")
    assert run["commit_sha"] == deployed.commit_sha
    assert run["branch"] == deployed.branch
    items = _page(client)["items"]
    assert items[0]["status"] == "pending"
    assert items[0]["redeploy_of"] == source
    assert items[0]["id"] == body["deployment_id"]
    audit = AuditService(harness.uow()).list(action="deployment.redeploy")
    assert audit[0].target_id == str(source)
    assert audit[0].details["run_number"] == 2
    assert audit[0].actor_type.value == "user"


def test_redeploy_run_deploys_the_same_images_without_building_or_checkout(
    session: tuple[TestClient, str], deployed: Run, harness: Harness, ledger: DatabaseLedger
) -> None:
    client, csrf = session
    source = _page(client)["items"][0]["id"]
    _redeploy(client, csrf, source)
    harness.source.requests.clear()
    harness.runtime.builds.clear()
    deployer = LedgerDeployer(ledger)
    harness.env = replace(harness.env, deployer=deployer)
    queued = _queued_run(harness)
    outcome = harness.execute(queued)
    assert outcome.status is RunStatus.SUCCEEDED, outcome.message
    request = deployer.requests[0]
    assert [(s.service, s.image) for s in request.services] == [("web", WEB_TAG)]
    assert request.run_id == queued.id
    assert harness.source.requests == []
    assert harness.runtime.builds == []
    statuses = harness.stage_statuses(queued)
    assert statuses["Checkout"] is RunStatus.SKIPPED
    assert statuses["Redeploy"] is RunStatus.SUCCEEDED
    items = _page(client)["items"]
    assert [i["status"] for i in items] == ["succeeded", "succeeded"]
    assert items[0]["run_id"] == queued.id
    assert items[0]["redeploy_of"] == source


def _queued_run(harness: Harness) -> Run:
    with harness.uow() as uow:
        run = uow.runs.list_by_status(RunStatus.QUEUED)[0]
        assert run.trigger is RunTrigger.REDEPLOY
        return run


def test_a_failed_redeploy_run_fails_with_the_deployers_message(
    session: tuple[TestClient, str], deployed: Run, harness: Harness, ledger: DatabaseLedger
) -> None:
    client, csrf = session
    _redeploy(client, csrf, _page(client)["items"][0]["id"])
    harness.env = replace(harness.env, deployer=LedgerDeployer(ledger, fail="rolled back: boom"))
    outcome = harness.execute(_queued_run(harness))
    assert outcome.status is RunStatus.FAILED
    assert outcome.message is not None
    assert "rolled back: boom" in outcome.message
    assert _page(client)["items"][0]["status"] == "rolled_back"


def test_a_pending_redeploy_whose_run_was_cancelled_shows_as_failed(
    session: tuple[TestClient, str], deployed: Run, harness: Harness
) -> None:
    client, csrf = session
    body = _redeploy(client, csrf, _page(client)["items"][0]["id"]).json()
    cancelled = client.post(
        f"/api/runs/{body['run']['id']}/cancel", headers=mutation_headers(client, csrf)
    )
    assert cancelled.status_code == 200
    item = _page(client)["items"][0]
    assert item["status"] == "failed"
    assert item["message"] == "cancelled before it started"


def test_redeploy_is_refused_for_unsuccessful_or_foreign_deployments(
    session: tuple[TestClient, str], deployed: Run, ledger: DatabaseLedger, harness: Harness
) -> None:
    client, csrf = session
    failed = ledger.begin("demo", "development", deployed.id, "a" * 40)
    ledger.finish(failed, DeploymentStatus.ROLLED_BACK, "rolled back")
    assert _redeploy(client, csrf, failed).status_code == 409
    assert _redeploy(client, csrf, 9999).status_code == 404
    production = client.post(
        f"/api/projects/demo/environments/{PROD.value}/deployments/1/redeploy",
        headers=mutation_headers(client, csrf),
    )
    assert production.status_code == 404


def test_redeploy_needs_csrf_and_the_deploy_scope(
    session: tuple[TestClient, str], deployed: Run, container: AppContainer, clock: FakeClock
) -> None:
    client, csrf = session
    source = _page(client)["items"][0]["id"]
    assert client.post(f"{HISTORY}/{source}/redeploy").status_code == 403

    def bearer(scopes: list[str]) -> dict[str, str]:
        body = {"name": "agent", "scopes": scopes}
        created = client.post("/api/tokens", json=body, headers=mutation_headers(client, csrf))
        return {"Authorization": f"Bearer {created.json()['token']}"}

    token_client = make_client(container)
    read_only = bearer(["read"])
    assert token_client.get(HISTORY, headers=read_only).status_code == 200
    denied = token_client.post(f"{HISTORY}/{source}/redeploy", headers=read_only)
    assert denied.status_code == 403
    allowed = token_client.post(f"{HISTORY}/{source}/redeploy", headers=bearer(["deploy"]))
    assert allowed.status_code == 201
    audit = AuditService(container.uow()).list(action="deployment.redeploy")
    assert audit[0].actor_type.value == "token"
