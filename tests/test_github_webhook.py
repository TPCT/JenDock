"""``POST /api/github/webhook``: signature, size, routing, auto-deploy and idempotency."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select

from backend.api.container import AppContainer
from backend.domain.enums import PipelineSource, RunStatus, RunTrigger
from backend.repositories.models import Run, WebhookDelivery
from backend.services.audit import AuditService
from backend.services.environments import EnvironmentService, EnvironmentUpdate
from backend.services.pipelines import PipelineService
from backend.services.projects import ProjectService
from tests.api_support import FakeClock, make_client, make_container, make_settings
from tests.conftest import DEV, PROD
from tests.github_support import (
    DELIVERY,
    SHA_A,
    SHA_B,
    ZERO,
    FakeGithub,
    push_payload,
    sign,
    webhook_headers,
)

URL = "/api/github/webhook"
MAX_BODY = 1024 * 1024


@pytest.fixture
def hook_container(tmp_path: Path, engine: Engine, clock: FakeClock) -> AppContainer:
    return make_container(make_settings(tmp_path), engine, clock, github=FakeGithub())


@pytest.fixture
def hook(hook_container: AppContainer) -> TestClient:
    return make_client(hook_container)


@pytest.fixture
def auto(hook_container: AppContainer, project: str) -> str:
    """The demo project with auto-deploy on for development only."""
    EnvironmentService(hook_container.uow()).update(
        project, DEV, EnvironmentUpdate(auto_deploy=True)
    )
    return project


def _runs(container: AppContainer) -> list[Run]:
    with container.uow() as uow:
        return list(uow.session.scalars(select(Run).order_by(Run.id)))


def _deliveries(container: AppContainer) -> int:
    with container.uow() as uow:
        return uow.session.scalar(select(func.count()).select_from(WebhookDelivery)) or 0


def _post(client: TestClient, body: bytes, **kwargs: object) -> tuple[int, dict[str, object]]:
    response = client.post(URL, content=body, headers=webhook_headers(body, **kwargs))  # type: ignore[arg-type]
    data: dict[str, object] = response.json()
    return response.status_code, data


# --- signature -------------------------------------------------------------------------------


def test_ping_with_a_good_signature(hook: TestClient, hook_container: AppContainer) -> None:
    body = b'{"zen": "Keep it logically awesome.", "repository": {"full_name": "octo/demo"}}'
    status, data = _post(hook, body, event="ping")
    assert (status, data["outcome"]) == (200, "pong")
    assert _deliveries(hook_container) == 1


def test_a_wrong_signature_is_rejected_and_nothing_happens(
    hook: TestClient, hook_container: AppContainer, auto: str
) -> None:
    body = push_payload()
    forged = sign(body, b"another-secret")
    status, data = _post(hook, body, signature=forged)
    assert status == 401
    assert data["error"]["code"] == "unauthenticated"  # type: ignore[index]
    assert _runs(hook_container) == []
    assert _deliveries(hook_container) == 0


def test_a_signature_over_a_different_body_is_rejected(
    hook: TestClient, hook_container: AppContainer, auto: str
) -> None:
    signed = sign(push_payload(after=SHA_B))
    status, _ = _post(hook, push_payload(after=SHA_A), signature=signed)
    assert status == 401
    assert _runs(hook_container) == []


@pytest.mark.parametrize(
    "signature", ["", "sha256=", "sha1=" + "0" * 40, "sha256=" + "G" * 64, "x"]
)
def test_a_missing_or_malformed_signature_is_rejected(
    hook: TestClient, auto: str, signature: str
) -> None:
    body = push_payload()
    response = hook.post(URL, content=body, headers=webhook_headers(body, signature=signature))
    assert response.status_code == 401


def test_no_signature_header_at_all(hook: TestClient, auto: str) -> None:
    body = push_payload()
    headers = webhook_headers(body)
    del headers["X-Hub-Signature-256"]
    assert hook.post(URL, content=body, headers=headers).status_code == 401


def test_the_endpoint_needs_no_session_and_ignores_cookies_and_origin(
    hook: TestClient, hook_container: AppContainer, auto: str
) -> None:
    body = push_payload()
    headers = webhook_headers(body) | {"Origin": "https://evil.example"}
    response = hook.post(URL, content=body, headers=headers)
    assert response.status_code == 202


def test_the_worker_being_down_answers_503(
    tmp_path: Path, engine: Engine, clock: FakeClock, auto: str
) -> None:
    down = make_client(make_container(make_settings(tmp_path), engine, clock))
    body = push_payload()
    response = down.post(URL, content=body, headers=webhook_headers(body))
    assert response.status_code == 503


# --- size and format -------------------------------------------------------------------------


def test_a_body_over_one_mebibyte_is_refused(hook: TestClient, auto: str) -> None:
    body = b'{"pad": "' + b"x" * MAX_BODY + b'"}'
    response = hook.post(URL, content=body, headers=webhook_headers(body))
    assert response.status_code == 413


def test_a_body_at_the_limit_is_read(hook: TestClient, auto: str) -> None:
    prefix = b'{"zen": "'
    body = prefix + b"x" * (MAX_BODY - len(prefix) - 2) + b'"}'
    assert len(body) == MAX_BODY
    status, data = _post(hook, body, event="ping")
    assert (status, data["outcome"]) == (200, "pong")


def test_chunked_bodies_are_bounded_too(hook: TestClient, auto: str) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(3):
            yield b"x" * (MAX_BODY // 2)

    headers = webhook_headers(b"unused")
    assert hook.post(URL, content=chunks(), headers=headers).status_code == 413


def test_only_json_is_accepted(hook: TestClient, auto: str) -> None:
    body = push_payload()
    headers = webhook_headers(body, content_type="application/x-www-form-urlencoded")
    assert hook.post(URL, content=body, headers=headers).status_code == 415


def test_an_unparseable_payload_is_422_and_not_remembered(
    hook: TestClient, hook_container: AppContainer
) -> None:
    status, data = _post(hook, b"{not json")
    assert status == 422
    assert data["error"]["code"] == "validation_error"  # type: ignore[index]
    assert _deliveries(hook_container) == 0


def test_a_deeply_nested_payload_is_422_not_a_crash(hook: TestClient) -> None:
    status, _ = _post(hook, b"[" * 100_000 + b"]" * 100_000)
    assert status == 422


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b'{"ref": "refs/heads/development"}',
        b'{"ref": 1, "after": "x", "repository": {"full_name": "octo/demo"}}',
        push_payload(after="not-a-sha"),
        push_payload(repository="no-slash"),
    ],
)
def test_a_malformed_push_payload_is_422(
    hook: TestClient, hook_container: AppContainer, auto: str, body: bytes
) -> None:
    status, _ = _post(hook, body)
    assert status == 422
    assert _runs(hook_container) == []
    assert _deliveries(hook_container) == 0


@pytest.mark.parametrize("delivery", [None, "", "short", "has space in it!", "x" * 65])
def test_a_missing_or_invalid_delivery_id_is_rejected(
    hook: TestClient, auto: str, delivery: str | None
) -> None:
    body = push_payload()
    response = hook.post(URL, content=body, headers=webhook_headers(body, delivery=delivery))
    assert response.status_code == 422


# --- routing ---------------------------------------------------------------------------------


def test_push_to_an_auto_deploy_branch_queues_a_pinned_push_run(
    hook: TestClient, hook_container: AppContainer, auto: str
) -> None:
    status, data = _post(hook, push_payload(after=SHA_B))
    assert (status, data["outcome"]) == (202, "queued")
    [run] = _runs(hook_container)
    assert data["runs"] == [run.id]
    assert run.trigger is RunTrigger.PUSH
    assert run.commit_sha == SHA_B
    assert run.branch == "development"
    assert run.status is RunStatus.QUEUED
    assert run.number == 1
    assert run.triggered_by is None
    assert run.triggered_by_token_id is None


def test_the_provisional_snapshot_is_the_latest_stored_pipeline(
    hook: TestClient, hook_container: AppContainer, auto: str
) -> None:
    text = (
        "pipeline {\n  agent none\n  stages {\n    stage('A') {\n"
        "      steps { echo 'x' }\n    }\n  }\n}\n"
    )
    PipelineService(hook_container.uow()).save(auto, DEV, text)
    _post(hook, push_payload())
    [run] = _runs(hook_container)
    assert run.pipeline_source is PipelineSource.PLATFORM
    assert run.pipeline_text.strip() == text.strip()


def test_push_with_auto_deploy_off_is_recorded_without_a_run(
    hook: TestClient, hook_container: AppContainer, project: str
) -> None:
    status, data = _post(hook, push_payload(ref="refs/heads/development"))
    assert (status, data["outcome"]) == (202, "recorded")
    assert data["runs"] == []
    assert _runs(hook_container) == []
    assert _deliveries(hook_container) == 1
    events = AuditService(hook_container.uow()).list(action="github.webhook")
    assert events[0].details["outcome"] == "recorded"
    # The delivery remembers the branch, which is what the dashboard's "waiting for a manual
    # deploy" card matches against the environments.
    uow = hook_container.uow()
    with uow:
        assert uow.session.scalar(select(WebhookDelivery.branch)) == "development"


def test_each_environment_follows_its_own_branch(
    hook: TestClient, hook_container: AppContainer, auto: str
) -> None:
    EnvironmentService(hook_container.uow()).update(auto, PROD, EnvironmentUpdate(auto_deploy=True))
    _post(hook, push_payload(ref="refs/heads/production", after=SHA_B), delivery="prod-delivery-1")
    [run] = _runs(hook_container)
    assert (run.branch, run.commit_sha) == ("production", SHA_B)
    assert run.environment_id != 0
    with hook_container.uow() as uow:
        environment = uow.environments.get(run.environment_id)
        assert environment is not None
        assert environment.name is PROD


def test_changing_the_environment_branch_changes_the_routing(
    hook: TestClient, hook_container: AppContainer, auto: str
) -> None:
    EnvironmentService(hook_container.uow()).update(auto, DEV, EnvironmentUpdate(branch="main"))
    status, data = _post(
        hook, push_payload(ref="refs/heads/development"), delivery="delivery-old-1"
    )
    assert (status, data["outcome"]) == (202, "ignored")
    status, data = _post(hook, push_payload(ref="refs/heads/main"), delivery="delivery-new-1")
    assert (status, data["outcome"]) == (202, "queued")


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (push_payload(after=ZERO, deleted=True), "branch deleted"),
        (push_payload(after=ZERO), "branch deleted"),
        (push_payload(deleted=True), "branch deleted"),
        (push_payload(ref="refs/tags/v1.0"), "not a branch push"),
        (push_payload(ref="refs/heads/feature/other"), "no environment tracks this branch"),
        (push_payload(repository="octo/other"), "repository is not allowlisted"),
    ],
)
def test_pushes_that_must_not_run(
    hook: TestClient, hook_container: AppContainer, auto: str, body: bytes, reason: str
) -> None:
    status, data = _post(hook, body)
    assert (status, data["outcome"], data["reason"]) == (202, "ignored", reason)
    assert _runs(hook_container) == []
    assert _deliveries(hook_container) == 1


def test_the_repository_match_ignores_case(
    hook: TestClient, hook_container: AppContainer, auto: str
) -> None:
    status, data = _post(hook, push_payload(repository="Octo/DEMO"))
    assert (status, data["outcome"]) == (202, "queued")


def test_archived_projects_are_not_allowlisted(
    hook: TestClient, hook_container: AppContainer, auto: str
) -> None:
    ProjectService(hook_container.uow()).archive(auto)
    status, data = _post(hook, push_payload())
    assert (status, data["outcome"]) == (202, "ignored")
    assert _runs(hook_container) == []


def test_two_projects_on_one_repository_both_run(
    hook: TestClient, hook_container: AppContainer, auto: str
) -> None:
    ProjectService(hook_container.uow()).create("twin", "Twin", "octo/demo")
    EnvironmentService(hook_container.uow()).update(
        "twin", DEV, EnvironmentUpdate(branch="development", auto_deploy=True)
    )
    _, data = _post(hook, push_payload())
    assert len(data["runs"]) == 2  # type: ignore[arg-type]


@pytest.mark.parametrize("event", ["issues", "pull_request", "workflow_run", "create"])
def test_other_events_are_signed_and_ignored(
    hook: TestClient, hook_container: AppContainer, auto: str, event: str
) -> None:
    status, data = _post(hook, push_payload(), event=event)
    assert (status, data["outcome"]) == (202, "ignored")
    assert _runs(hook_container) == []


# --- idempotency -----------------------------------------------------------------------------


def test_a_replayed_delivery_changes_nothing(
    hook: TestClient, hook_container: AppContainer, auto: str
) -> None:
    body = push_payload()
    first = _post(hook, body)
    again = _post(hook, body)
    assert first[1]["outcome"] == "queued"
    assert (again[0], again[1]["outcome"]) == (200, "duplicate")
    assert len(_runs(hook_container)) == 1
    assert _deliveries(hook_container) == 1
    assert len(AuditService(hook_container.uow()).list(action="github.webhook")) == 1


def test_a_replay_with_a_different_body_is_still_a_duplicate(
    hook: TestClient, hook_container: AppContainer, auto: str
) -> None:
    _post(hook, push_payload(after=SHA_A))
    status, data = _post(hook, push_payload(after=SHA_B))
    assert (status, data["outcome"]) == (200, "duplicate")
    assert len(_runs(hook_container)) == 1


def test_different_deliveries_of_the_same_push_each_queue_a_run(
    hook: TestClient, hook_container: AppContainer, auto: str
) -> None:
    _post(hook, push_payload(), delivery="delivery-one-1")
    _post(hook, push_payload(), delivery="delivery-two-2")
    assert [r.number for r in _runs(hook_container)] == [1, 2]


def test_old_deliveries_are_forgotten_after_retention(
    hook: TestClient, hook_container: AppContainer, clock: FakeClock, auto: str
) -> None:
    _post(hook, push_payload(), delivery="delivery-old-1")
    clock.advance(days=31)
    _post(hook, push_payload(), delivery="delivery-new-1")
    assert _deliveries(hook_container) == 1


# --- audit -----------------------------------------------------------------------------------


def test_the_delivery_and_the_run_are_audited(
    hook: TestClient, hook_container: AppContainer, auto: str
) -> None:
    _post(hook, push_payload(after=SHA_B))
    audit = AuditService(hook_container.uow())
    [hook_event] = audit.list(action="github.webhook")
    assert hook_event.actor_id == "github"
    assert hook_event.target_id == DELIVERY
    assert hook_event.details["outcome"] == "queued"
    assert hook_event.details["repository"] == "octo/demo"
    assert hook_event.details["branch"] == "development"
    [run_event] = audit.list(action="run.create")
    assert run_event.details["trigger"] == "push"
    assert run_event.details["commit"] == SHA_B
    assert "secret" not in repr([e.details for e in audit.list()]).lower()


# --- failed-signature limiter ----------------------------------------------------------------


def test_repeated_bad_signatures_are_limited_per_ip_before_the_worker(
    hook: TestClient, hook_container: AppContainer, clock: FakeClock, auto: str
) -> None:
    github = hook_container.github
    calls: list[int] = []
    original = github.verify_webhook

    def counting(signature: str, body: bytes) -> bool:
        calls.append(1)
        return original(signature, body)

    github.verify_webhook = counting  # type: ignore[method-assign]
    body = push_payload()
    bad = webhook_headers(body, signature=sign(body, b"wrong"))
    for _ in range(30):
        assert hook.post(URL, content=body, headers=bad).status_code == 401
    blocked = hook.post(URL, content=body, headers=bad)
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) >= 1
    assert len(calls) == 30  # the 31st never reached the worker
    clock.advance(seconds=61)
    assert hook.post(URL, content=body, headers=webhook_headers(body)).status_code == 202


def test_valid_deliveries_are_never_counted(hook: TestClient, auto: str) -> None:
    for number in range(40):
        body = push_payload()
        headers = webhook_headers(body, delivery=f"delivery-{number:04d}-x")
        assert hook.post(URL, content=body, headers=headers).status_code == 202


def test_malformed_signatures_count_as_failures(hook: TestClient, auto: str) -> None:
    body = push_payload()
    headers = webhook_headers(body, signature="garbage")
    for _ in range(30):
        assert hook.post(URL, content=body, headers=headers).status_code == 401
    assert hook.post(URL, content=body, headers=headers).status_code == 429
