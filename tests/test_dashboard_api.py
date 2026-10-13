"""``GET /api/dashboard``: cards, empty states and the fixed query count."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event

from backend.api.container import AppContainer
from backend.containers.wire import HostSampleView, HostTelemetryView
from backend.domain.actor import Actor
from backend.domain.enums import DeploymentStatus, PipelineSource, RunStatus, RunTrigger
from backend.domain.errors import WorkerUnavailableError
from backend.repositories.models import (
    Deployment,
    Domain,
    DomainCertificate,
    Run,
    WebhookDelivery,
)
from backend.services.projects import ProjectService
from tests.api_support import FakeClock, login, make_client, mutation_headers


class FakeContainers:
    """Only the host telemetry call of the container gateway."""

    def __init__(self, sample: HostSampleView | None) -> None:
        self.sample = sample

    def host_telemetry(self, _actor: Actor) -> HostTelemetryView:
        if self.sample is None:
            raise WorkerUnavailableError("worker is not reachable")
        return HostTelemetryView(latest=self.sample, history=(self.sample,))


def _sample(clock: FakeClock) -> HostSampleView:
    gib = 1024**3
    return HostSampleView(
        timestamp=clock.now,
        cpu_percent=37.5,
        cpu_count=8,
        load_1=1.25,
        load_5=1.0,
        load_15=0.5,
        memory_total=16 * gib,
        memory_used=4 * gib,
        memory_available=12 * gib,
        swap_total=0,
        swap_free=0,
        disk_total=100 * gib,
        disk_used=80 * gib,
        disk_free=20 * gib,
    )


@pytest.fixture
def host(clock: FakeClock) -> FakeContainers:
    return FakeContainers(_sample(clock))


@pytest.fixture
def signed_in(
    container: AppContainer, admin: int, host: FakeContainers
) -> tuple[TestClient, AppContainer]:
    wired = replace(container, containers=host)  # type: ignore[arg-type]
    client = make_client(wired)
    login(client)
    return client, wired


def _run(env_id: int, number: int, status: RunStatus, **extra: Any) -> Run:
    return Run(
        environment_id=env_id,
        number=number,
        trigger=extra.pop("trigger", RunTrigger.PUSH),
        branch="development",
        commit_sha=extra.pop("commit_sha", "a" * 40),
        pipeline_source=PipelineSource.PLATFORM,
        pipeline_text="pipeline",
        status=status,
        **extra,
    )


def _make_projects(container: AppContainer, count: int, start: int = 0) -> list[list[int]]:
    """``count`` projects; returns the [development, production] environment ids of each."""
    service = ProjectService(container.uow(), container.clock)
    ids = []
    for index in range(start, start + count):
        detail = service.create(f"proj-{index}", f"Project {index}", f"octo/proj-{index}")
        ids.append([env.id for env in detail.environments])
    return ids


def _populate(container: AppContainer, envs: list[list[int]], clock: FakeClock) -> None:
    uow = container.uow()
    with uow:
        for number, (dev, prod) in enumerate(envs, start=1):
            moment = clock.now - timedelta(minutes=number)
            ok = _run(
                dev,
                1,
                RunStatus.SUCCEEDED,
                created_at=moment,
                started_at=moment,
                finished_at=moment + timedelta(seconds=42),
                triggered_by=1,
            )
            failed = _run(
                prod,
                1,
                RunStatus.FAILED,
                created_at=moment,
                started_at=moment,
                finished_at=moment + timedelta(seconds=5),
                message="build failed",
            )
            running = _run(dev, 2, RunStatus.RUNNING, created_at=moment, started_at=moment)
            uow.session.add_all([ok, failed, running])
            uow.session.flush()
            uow.session.add(
                Deployment(
                    run_id=ok.id,
                    environment_id=dev,
                    status=DeploymentStatus.SUCCEEDED,
                    commit_sha="a" * 40,
                    created_at=moment,
                    started_at=moment,
                    finished_at=moment + timedelta(seconds=40),
                )
            )


def _get(client: TestClient) -> dict[str, Any]:
    response = client.get("/api/dashboard")
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


def test_requires_authentication(client: TestClient) -> None:
    assert client.get("/api/dashboard").status_code == 401


def test_a_read_token_may_call_it(
    signed_in: tuple[TestClient, AppContainer],
) -> None:
    client, _ = signed_in
    csrf = client.get("/api/auth/session").json()["csrf_token"]
    token = client.post(
        "/api/tokens",
        json={"name": "r", "scopes": ["read"]},
        headers=mutation_headers(client, csrf),
    ).json()["token"]
    bare = TestClient(client.app, base_url=str(client.base_url))
    assert (
        bare.get("/api/dashboard", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    )


def test_empty_installation(signed_in: tuple[TestClient, AppContainer]) -> None:
    client, _ = signed_in
    body = _get(client)
    assert body["project_count"] == 0
    assert body["domain_count"] == 0
    for card in ("deployments", "attention", "manual_deploys", "certificates"):
        assert body[card] == []
    assert body["host"]["cpu_percent"] == 37.5
    assert body["host_error"] is None


def test_host_summary_has_percentages(signed_in: tuple[TestClient, AppContainer]) -> None:
    client, _ = signed_in
    host = _get(client)["host"]
    assert host["memory_percent"] == 25.0
    assert host["disk_percent"] == 80.0
    assert host["cpu_count"] == 8


def test_unreachable_worker_only_empties_the_host_card(
    signed_in: tuple[TestClient, AppContainer], host: FakeContainers
) -> None:
    client, _ = signed_in
    host.sample = None
    body = _get(client)
    assert body["host"] is None
    assert body["host_error"]


def test_recent_deployments_with_who_and_duration(
    signed_in: tuple[TestClient, AppContainer], clock: FakeClock
) -> None:
    client, container = signed_in
    envs = _make_projects(container, 1)
    _populate(container, envs, clock)
    body = _get(client)
    deployment = body["deployments"][0]
    assert deployment["project"] == "proj-0"
    assert deployment["environment"] == "development"
    assert deployment["status"] == "succeeded"
    assert deployment["commit_sha"] == "a" * 40
    assert deployment["trigger"] == "push"
    assert deployment["triggered_by"] == "admin"
    assert deployment["duration_seconds"] == 40.0
    assert deployment["run_number"] == 1
    assert isinstance(deployment["run_id"], int)


def test_attention_lists_active_runs_and_failing_environments(
    signed_in: tuple[TestClient, AppContainer], clock: FakeClock
) -> None:
    client, container = signed_in
    envs = _make_projects(container, 1)
    _populate(container, envs, clock)
    body = _get(client)
    statuses = sorted((item["status"], item["environment"]) for item in body["attention"])
    assert statuses == [("failed", "production"), ("running", "development")]
    failed = next(item for item in body["attention"] if item["status"] == "failed")
    assert failed["message"] == "build failed"


def test_an_old_failure_is_not_attention_once_a_newer_run_succeeded(
    signed_in: tuple[TestClient, AppContainer], clock: FakeClock
) -> None:
    client, container = signed_in
    ((dev, _prod),) = _make_projects(container, 1)
    uow = container.uow()
    with uow:
        uow.session.add(_run(dev, 1, RunStatus.FAILED, finished_at=clock.now))
        uow.session.add(_run(dev, 2, RunStatus.SUCCEEDED, finished_at=clock.now))
    assert _get(client)["attention"] == []


def test_pushes_to_environments_with_auto_deploy_off(
    signed_in: tuple[TestClient, AppContainer], clock: FakeClock
) -> None:
    client, container = signed_in
    ((dev, _prod),) = _make_projects(container, 1)
    uow = container.uow()
    with uow:
        for index, (outcome, branch, age) in enumerate(
            [
                ("recorded", "development", timedelta(hours=1)),
                ("recorded", "development", timedelta(hours=2)),
                ("queued", "development", timedelta(hours=1)),
                ("recorded", "development", timedelta(days=30)),
                ("recorded", "unrelated", timedelta(hours=1)),
            ]
        ):
            uow.session.add(
                WebhookDelivery(
                    delivery_id=f"d{index}",
                    event="push",
                    repository="octo/proj-0",
                    outcome=outcome,
                    branch=branch,
                    received_at=clock.now - age,
                )
            )
    manual = _get(client)["manual_deploys"]
    assert len(manual) == 1
    assert manual[0]["project"] == "proj-0"
    assert manual[0]["branch"] == "development"
    assert manual[0]["pushes"] == 2
    # Turning auto-deploy on removes the hint.
    uow = container.uow()
    with uow:
        env = uow.environments.get(dev)
        assert env is not None
        env.auto_deploy = True
    assert _get(client)["manual_deploys"] == []


def test_certificates_expiring_soon_or_expired(
    signed_in: tuple[TestClient, AppContainer], clock: FakeClock
) -> None:
    client, container = signed_in
    ((dev, _prod),) = _make_projects(container, 1)
    uow = container.uow()
    with uow:
        rows: list[DomainCertificate] = []
        for name, days in (("soon", 10), ("later", 60), ("gone", -2)):
            domain = Domain(
                environment_id=dev,
                hostname=f"{name}.example.test",
                service="web",
                container_port=80,
                published_port=8080 + len(rows),
            )
            uow.session.add(domain)
            uow.session.flush()
            rows.append(
                DomainCertificate(
                    domain_id=domain.id,
                    serial="1",
                    fingerprint_sha256="f" * 64,
                    challenge="http-01",
                    not_before=clock.now - timedelta(days=60),
                    not_after=clock.now + timedelta(days=days),
                )
            )
        uow.session.add_all(rows)
    body = _get(client)
    assert [(c["hostname"], c["expired"]) for c in body["certificates"]] == [
        ("gone.example.test", True),
        ("soon.example.test", False),
    ]
    assert body["domain_count"] == 3


def test_cards_are_bounded(signed_in: tuple[TestClient, AppContainer], clock: FakeClock) -> None:
    client, container = signed_in
    _populate(container, _make_projects(container, 15), clock)
    body = _get(client)
    assert body["project_count"] == 15
    assert len(body["deployments"]) == 10
    assert len(body["attention"]) <= 20


def _count_statements(engine: Engine, client: TestClient) -> int:
    statements: list[str] = []

    def record(_conn: object, _cursor: object, statement: str, *_rest: object) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        _get(client)
    finally:
        event.remove(engine, "before_cursor_execute", record)
    return len(statements)


def test_query_count_does_not_grow_with_the_data(
    signed_in: tuple[TestClient, AppContainer], engine: Engine, clock: FakeClock
) -> None:
    client, container = signed_in
    _populate(container, _make_projects(container, 2), clock)
    small = _count_statements(engine, client)
    _populate(container, _make_projects(container, 10, start=2), clock)
    large = _count_statements(engine, client)
    assert small == large
    assert small <= 12  # session lookup, a handful of cards, no per-row queries
