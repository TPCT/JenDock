"""What the audit log and the home dashboard show to owners, admins and members."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from backend.containers.wire import HostSampleView, HostTelemetryView
from backend.domain.access import GlobalRole
from backend.domain.actor import Actor
from backend.domain.audit import AuditTarget
from backend.domain.enums import (
    ActorType,
    DeploymentStatus,
    EnvironmentName,
    PipelineSource,
    RunStatus,
    RunTrigger,
)
from backend.repositories.models import Deployment, Domain, DomainCertificate, Run
from backend.services.audit import AuditService
from tests.access_support import SYSTEM, add_user, sign_in
from tests.access_world import World

DEV, PROD = EnvironmentName.DEVELOPMENT, EnvironmentName.PRODUCTION


def _env_id(world: World, slug: str, env: EnvironmentName) -> int:
    with world.container.uow() as uow:
        environment = uow.environments.get_by_project_slug(slug, env)
        assert environment is not None
        return environment.id


def _record(world: World, actor_id: int, action: str, target: AuditTarget) -> None:
    AuditService(world.container.uow(), world.container.clock).record(
        Actor(ActorType.USER, str(actor_id)), action, target, {"name": "X"}
    )


def _actions(response: Any) -> list[tuple[str, str]]:
    assert response.status_code == 200, response.text
    return [(item["action"], item["target_id"]) for item in response.json()["items"]]


@pytest.fixture
def events(world: World) -> dict[str, int]:
    """Events by the viewer on both projects, by the owner, and by the viewer on a token."""
    demo_dev = _env_id(world, "demo", DEV)
    other_dev = _env_id(world, "other", DEV)
    viewer = world["viewer"].user_id
    _record(world, viewer, "secret.reveal", AuditTarget("environment", str(demo_dev)))
    _record(world, viewer, "secret.reveal", AuditTarget("environment", str(other_dev)))
    _record(world, world.owner.user_id, "secret.rotate", AuditTarget("environment", str(demo_dev)))
    return {"demo_dev": demo_dev, "other_dev": other_dev}


def test_owner_sees_everything(world: World, events: dict[str, int]) -> None:
    found = _actions(world.owner.get("/api/audit?limit=200"))
    assert ("secret.rotate", str(events["demo_dev"])) in found
    assert ("secret.reveal", str(events["other_dev"])) in found
    assert len(found) >= 6  # the logins and user.create events of the world, plus three above


def test_admin_sees_everything_too(world: World, events: dict[str, int]) -> None:
    admin_id = add_user(world.container, "boss", GlobalRole.ADMIN)
    boss = sign_in(world.container, "boss")
    found = _actions(boss.get("/api/audit?limit=200"))
    assert ("secret.reveal", str(events["other_dev"])) in found
    assert admin_id > 0


def test_member_sees_only_their_own_activity_on_what_they_can_see(
    world: World, events: dict[str, int]
) -> None:
    found = _actions(world["viewer"].get("/api/audit?limit=200"))
    assert ("secret.reveal", str(events["demo_dev"])) in found  # their own, on their project
    assert ("secret.reveal", str(events["other_dev"])) not in found  # a project without a grant
    assert ("secret.rotate", str(events["demo_dev"])) not in found  # somebody else's
    assert ("auth.login", str(world["viewer"].user_id)) in found  # their own sign-in
    other_users = [
        t for a, t in found if a.startswith("auth.") and t != str(world["viewer"].user_id)
    ]
    assert other_users == []


def test_member_without_grants_only_sees_their_account(
    world: World, events: dict[str, int]
) -> None:
    found = _actions(world["nobody"].get("/api/audit?limit=200"))
    assert found
    assert {target for _action, target in found} == {str(world["nobody"].user_id)}


def test_revoking_a_grant_hides_the_members_old_project_events(
    world: World, events: dict[str, int]
) -> None:
    viewer = world["viewer"]
    assert ("secret.reveal", str(events["demo_dev"])) in _actions(viewer.get("/api/audit"))
    service = world.container.access_service()
    for record in service.for_user(viewer.user_id):
        service.remove_grant(record.project, record.id, SYSTEM)
    assert ("secret.reveal", str(events["demo_dev"])) not in _actions(viewer.get("/api/audit"))


def test_the_member_scope_applies_to_the_export_and_security_log(
    world: World, events: dict[str, int]
) -> None:
    csv_text = world["viewer"].get("/api/audit/export.csv").text
    rows = [line for line in csv_text.splitlines()[1:] if "secret.reveal" in line]
    assert len(rows) == 1
    assert f",environment,{events['demo_dev']}," in rows[0]
    log = _actions(world["viewer"].get("/api/me/security-log"))
    assert ("secret.reveal", str(events["other_dev"])) not in log


def test_member_filters_cannot_widen_the_scope(world: World, events: dict[str, int]) -> None:
    query = f"/api/audit?project=other&target_type=environment&target_id={events['other_dev']}"
    assert _actions(world["viewer"].get(query)) == []
    system = _actions(world["viewer"].get("/api/audit?actor_type=system"))
    assert {target for _action, target in system} <= {str(world["viewer"].user_id)}


def test_tokens_cannot_read_the_audit_log(world: World) -> None:
    created = world.owner.post("/api/tokens", {"name": "t", "scopes": ["admin"]}).json()
    bearer = {"Authorization": f"Bearer {created['token']}"}
    assert world.owner.client.get("/api/audit", headers=bearer).status_code in {401, 403}


# --- dashboard -------------------------------------------------------------------------------


def _seed(world: World) -> None:
    """A deployment, a running run and a failing one per project, plus a certificate each."""
    now = world.container.clock()
    with world.container.uow() as uow:
        for slug in ("demo", "other"):
            for env in (DEV, PROD):
                env_row = uow.environments.get_by_project_slug(slug, env)
                assert env_row is not None
                run = Run(
                    environment_id=env_row.id,
                    number=50,
                    trigger=RunTrigger.PUSH,
                    branch=env_row.branch,
                    commit_sha="a" * 40,
                    pipeline_source=PipelineSource.PLATFORM,
                    pipeline_text="p",
                    status=RunStatus.RUNNING,
                    created_at=now,
                    started_at=now,
                )
                uow.session.add(run)
                uow.session.flush()
                uow.session.add(
                    Deployment(
                        run_id=run.id,
                        environment_id=env_row.id,
                        status=DeploymentStatus.SUCCEEDED,
                        commit_sha="a" * 40,
                        created_at=now,
                    )
                )
                domain = Domain(
                    environment_id=env_row.id,
                    hostname=f"{env.value}.{slug}.example.test",
                    service="web",
                    container_port=80,
                    published_port=8000 + len(slug) + len(env.value),
                )
                uow.session.add(domain)
                uow.session.flush()
                uow.session.add(
                    DomainCertificate(
                        domain_id=domain.id,
                        serial="1",
                        fingerprint_sha256="f" * 64,
                        challenge="http-01",
                        not_before=now - timedelta(days=60),
                        not_after=now + timedelta(days=5),
                    )
                )


@pytest.fixture
def seeded(world: World) -> World:
    _seed(world)
    sample = HostSampleView(
        timestamp=world.container.clock(),
        cpu_percent=10.0,
        cpu_count=2,
        load_1=0.5,
        load_5=0.5,
        load_15=0.5,
        memory_total=100,
        memory_used=50,
        memory_available=50,
        swap_total=0,
        swap_free=0,
        disk_total=100,
        disk_used=10,
        disk_free=90,
    )
    reading = HostTelemetryView(latest=sample, history=(sample,))
    world.fake.__dict__["host_telemetry"] = lambda _actor: reading
    return world


def _hosts(body: dict[str, Any]) -> list[str]:
    return sorted(f"{d['project']}/{d['environment']}" for d in body["deployments"])


def test_owner_dashboard_covers_every_project(seeded: World) -> None:
    body = seeded.owner.get("/api/dashboard").json()
    assert body["project_count"] == 2
    assert _hosts(body) == [
        "demo/development",
        "demo/production",
        "other/development",
        "other/production",
    ]
    assert body["domain_count"] == 4
    assert len(body["certificates"]) == 4
    assert body["host"]["cpu_percent"] == 10.0


def test_project_viewer_sees_only_their_project(seeded: World) -> None:
    body = seeded["viewer"].get("/api/dashboard").json()
    assert body["project_count"] == 1
    assert _hosts(body) == ["demo/development", "demo/production"]
    assert {run["project"] for run in body["attention"]} == {"demo"}
    assert {c["project"] for c in body["certificates"]} == {"demo"}
    assert body["domain_count"] == 2
    assert body["host"] is not None  # holds a grant


def test_environment_grant_limits_every_card_to_that_environment(seeded: World) -> None:
    body = seeded["devonly"].get("/api/dashboard").json()
    assert _hosts(body) == ["demo/development"]
    assert {(run["project"], run["environment"]) for run in body["attention"]} == {
        ("demo", "development")
    }
    assert [c["environment"] for c in body["certificates"]] == ["development"]
    assert body["domain_count"] == 1


def test_a_member_without_grants_gets_an_empty_dashboard(seeded: World) -> None:
    body = seeded["nobody"].get("/api/dashboard").json()
    assert body["project_count"] == 0
    assert body["domain_count"] == 0
    for card in ("deployments", "attention", "manual_deploys", "certificates"):
        assert body[card] == []
    assert body["host"] is None
    assert body["host_error"] is None


def test_container_only_grant_sees_no_project_data(seeded: World) -> None:
    body = seeded["logs"].get("/api/dashboard").json()
    assert body["deployments"] == []
    assert body["attention"] == []
    assert body["certificates"] == []
