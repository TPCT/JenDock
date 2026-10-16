"""Resource-limit endpoints through a real worker RPC server on fake Docker; access rules."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine

from backend.adapters.docker_stats import ContainerStats
from backend.api.container import AppContainer
from backend.domain.access import RULES, AccessRole, Action
from backend.domain.actor import Actor
from backend.domain.enums import ActorType, EnvironmentName
from backend.domain.resources import GIB, MIB
from backend.repositories.database import UnitOfWork, create_session_factory
from backend.services.audit import AuditService
from backend.services.secret_cipher import SecretCipher
from backend.services.secrets import SecretService
from backend.worker.container_handlers import build_container_handlers
from backend.worker.handlers import build_handlers
from backend.worker.rpc import BadRequestError, RpcCall, RpcServer
from tests.access_support import Session, add_user, grant, sign_in
from tests.access_world import World
from tests.api_support import (
    FakeClock,
    create_user,
    login,
    make_client,
    make_container,
    make_settings,
    mutation_headers,
)
from tests.container_harness import Stack, build_stack

DEMO = "/api/projects/demo"
BASE = f"{DEMO}/environments/development/resources"
PROD_BASE = f"{DEMO}/environments/production/resources"


@dataclass
class Wired:
    """The API wired to a worker (real RPC socket) with fake Docker."""

    container: AppContainer
    client: TestClient
    csrf: str
    stack: Stack
    engine: Engine

    def headers(self) -> dict[str, str]:
        return mutation_headers(self.client, self.csrf)

    def audit(self, action: str) -> list[dict[str, object]]:
        return [dict(e.details) for e in AuditService(self.container.uow()).list(action=action)]


@pytest.fixture
def wired(
    tmp_path: Path, socket_dir: Path, engine: Engine, clock: FakeClock, project: str
) -> Iterator[Wired]:
    stack = build_stack(tmp_path, engine, project)
    factory = create_session_factory(engine)
    cipher = SecretCipher(tmp_path / "worker-only" / "secret.key")
    handlers = build_handlers(
        lambda: SecretService(UnitOfWork(factory), cipher), build_container_handlers(stack.services)
    )
    settings = make_settings(tmp_path, socket_path=socket_dir / "w.sock")
    rpc = RpcServer(settings.worker_socket_path, handlers)
    rpc.start()
    try:
        container = make_container(settings, engine, clock)
        create_user(container)
        client = make_client(container)
        yield Wired(container, client, login(client), stack, engine)
    finally:
        rpc.stop()


def _put(wired: Wired, service: str, body: Mapping[str, object]) -> Response:
    return wired.client.put(f"{BASE}/{service}", json=body, headers=wired.headers())


# --- the endpoints through the worker RPC ----------------------------------------------------


def test_overview_lists_the_host_and_every_service(wired: Wired) -> None:
    response = wired.client.get(BASE)
    assert response.status_code == 200
    assert response.json() == {
        "host": {"cpus": 4, "memory_bytes": 8 * GIB},
        "services": [
            {"service": "web", "limits": None, "updated_at": None, "origin": None},
            {"service": "worker", "limits": None, "updated_at": None, "origin": None},
        ],
    }


def test_save_then_read_back_and_apply_now(wired: Wired) -> None:
    body = {"cpus": 1.5, "memory_bytes": 512 * MIB, "memory_reservation_bytes": 128 * MIB}
    saved = _put(wired, "web", {**body, "apply_now": True})
    assert saved.status_code == 200
    result = saved.json()
    assert result["limits"] == {**body, "cpu_shares": None}
    assert [(a["id"], a["ok"]) for a in result["applied"]] == [(wired.stack.web_id, True)]
    listed = wired.client.get(BASE).json()["services"][0]
    assert listed["service"] == "web" and listed["limits"]["cpus"] == 1.5
    assert listed["updated_at"] is not None
    # the running container really got them
    live = wired.client.get(f"{DEMO}/environments/development/containers/{wired.stack.web_id}")
    assert live.json()["cpus"] == 1.5 and live.json()["memory_bytes"] == 512 * MIB
    assert wired.audit("container.resources.update")[0]["service"] == "web"


def test_save_without_apply_now_only_saves(wired: Wired) -> None:
    result = _put(wired, "web", {"cpus": 2}).json()
    assert result["applied"] == []
    assert not any(c[0] == "update_resources" for c in wired.stack.engine.calls)
    applied = wired.client.post(f"{BASE}/web/apply", headers=wired.headers())
    assert applied.status_code == 200 and applied.json()["applied"][0]["ok"] is True
    assert wired.stack.engine.calls[-1][0] == "update_resources"


def test_invalid_values_are_422_with_a_reason(wired: Wired) -> None:
    cases = [
        ({"cpus": 8}, "cannot exceed the host's 4 CPUs"),
        ({"memory_bytes": 5 * MIB}, "at least 6 MiB"),
        ({"memory_bytes": 16 * GIB}, "cannot exceed the host's 8 GiB"),
        ({}, "at least one limit"),
        (
            {"cpus": 1, "memory_bytes": 128 * MIB, "memory_reservation_bytes": 256 * MIB},
            "reservation cannot be larger",
        ),
        ({"cpus": 1, "cpu_shares": 1}, "cpu shares must be between"),
    ]
    for body, reason in cases:
        response = _put(wired, "web", body)
        assert response.status_code == 422, body
        assert reason in response.json()["error"]["message"], body
    for bad in ({"cpus": 0}, {"cpus": -1}, {"memory_bytes": 0}, {"bogus": 1}, {"cpus": "x"}):
        assert _put(wired, "web", bad).status_code == 422, bad
    assert wired.client.get(BASE).json()["services"][0]["limits"] is None


def test_unknown_or_malformed_services(wired: Wired) -> None:
    assert _put(wired, "nope", {"cpus": 1}).status_code == 404
    assert _put(wired, "-bad", {"cpus": 1}).status_code == 422
    assert wired.client.delete(f"{BASE}/web", headers=wired.headers()).status_code == 404
    assert wired.client.post(f"{BASE}/web/apply", headers=wired.headers()).status_code == 404


def test_remove_limit_with_and_without_apply_now(wired: Wired) -> None:
    _put(wired, "web", {"cpus": 1, "memory_bytes": GIB, "apply_now": True})
    plain = wired.client.delete(f"{BASE}/web", headers=wired.headers())
    assert plain.status_code == 200
    assert plain.json()["limits"] is None and plain.json()["applied"] == []
    _put(wired, "web", {"cpus": 1, "memory_bytes": GIB, "apply_now": True})
    lifted = wired.client.delete(f"{BASE}/web?apply_now=true", headers=wired.headers())
    assert lifted.json()["applied"][0]["ok"] is True
    assert lifted.json()["notes"] and "next deploy" in lifted.json()["notes"][0]
    assert wired.client.get(BASE).json()["services"][0]["limits"] is None


def test_a_container_that_docker_refuses_is_reported_not_hidden(wired: Wired) -> None:
    wired.stack.engine.fail["update_resources"] = "cannot update: boom"
    result = _put(wired, "web", {"cpus": 1, "apply_now": True}).json()
    assert result["applied"][0]["ok"] is False
    assert result["applied"][0]["error"] == "cannot update: boom"
    assert wired.client.get(BASE).json()["services"][0]["limits"]["cpus"] == 1  # still saved


def test_stats_report_usage_and_the_hosts_capacity(wired: Wired) -> None:
    _put(wired, "web", {"cpus": 2, "memory_bytes": 512 * MIB, "apply_now": True})
    wired.stack.engine.sample_stats = [
        ContainerStats(
            wired.stack.web_id, "demo-web-1", 87.5, 200 * MIB, 512 * MIB, 39.1, 0, 0, 0, 0, 5
        )
    ]
    response = wired.client.get(f"{BASE}/stats")
    assert response.status_code == 200
    assert response.json() == {
        "host": {"cpus": 4, "memory_bytes": 8 * GIB},
        "containers": [
            {
                "id": wired.stack.web_id,
                "name": "demo-dev-web-1",
                "service": "web",
                "cpu_percent": 87.5,
                "memory_used": 200 * MIB,
                "memory_percent": 39.1,
                "cpu_limit": 2.0,
                "memory_limit": 512 * MIB,
            }
        ],
    }


def test_docker_down_is_a_clear_409(wired: Wired) -> None:
    wired.stack.engine.fail["host_capacity"] = "docker is not usable"
    response = wired.client.get(BASE)
    assert response.status_code == 409 and "not usable" in response.json()["error"]["message"]


def test_the_worker_rejects_malformed_rpc_params(wired: Wired) -> None:
    handlers = build_container_handlers(wired.stack.services)
    actor = Actor(ActorType.USER, "1")
    base: dict[str, object] = {"slug": "demo", "env": "development", "service": "web"}
    for params in (
        {**base, "cpus": "lots"},
        {**base, "bogus": 1},
        {"slug": "demo", "env": "staging", "service": "web"},
        {"slug": "demo", "env": "development"},
    ):
        with pytest.raises(BadRequestError):
            handlers["resources.save"](RpcCall(actor, params))
    with pytest.raises(BadRequestError):
        handlers["resources.save"](RpcCall(None, {**base, "cpus": 1}))
    ok = handlers["resources.save"](RpcCall(actor, {**base, "cpus": 1}))
    assert isinstance(ok, dict) and ok["service"] == "web"


# --- token scopes -----------------------------------------------------------------------------


def _token(wired: Wired, scopes: list[str]) -> dict[str, str]:
    body = {"name": "t", "scopes": scopes}
    created = wired.client.post("/api/tokens", json=body, headers=wired.headers())
    return {"Authorization": f"Bearer {created.json()['token']}"}


def test_read_scope_reads_but_only_containers_scope_changes(wired: Wired) -> None:
    bare = TestClient(wired.client.app, base_url=str(wired.client.base_url))
    reader = _token(wired, ["read"])
    writer = _token(wired, ["containers"])
    assert bare.get(BASE, headers=reader).status_code == 200
    assert bare.get(f"{BASE}/stats", headers=reader).status_code == 200
    denied = bare.put(f"{BASE}/web", json={"cpus": 1}, headers=reader)
    assert denied.status_code == 403 and "missing scope: containers" in denied.text
    assert bare.delete(f"{BASE}/web", headers=reader).status_code == 403
    assert bare.post(f"{BASE}/web/apply", headers=reader).status_code == 403
    assert bare.put(f"{BASE}/web", json={"cpus": 1}, headers=writer).status_code == 200
    assert bare.get(BASE, headers=writer).status_code == 403  # containers alone cannot read


# --- who may change limits ---------------------------------------------------------------------


def _put_as(who: Session, base: str, body: Mapping[str, object] | None = None) -> int:
    return who.put(f"{base}/web", body or {"cpus": 1}).status_code


def test_changing_limits_needs_a_maintainer_on_the_environment(world: World) -> None:
    body: dict[str, object] = {"cpus": 1, "memory_bytes": 256 * MIB, "apply_now": True}
    assert _put_as(world["maintainer"], f"{DEMO}/environments/development/resources", body) == 200
    for who in ("viewer", "developer"):
        assert _put_as(world[who], f"{DEMO}/environments/development/resources", body) == 403
        assert (
            world[who].delete(f"{DEMO}/environments/development/resources/web").status_code == 403
        )
        post = world[who].post(f"{DEMO}/environments/development/resources/web/apply")
        assert post.status_code == 403
    # a member without any grant does not even learn that the project exists
    assert _put_as(world["nobody"], BASE) == 404
    assert world["nobody"].get(BASE).status_code == 404
    assert world["nobody"].delete(f"{BASE}/web").status_code == 404


def test_production_needs_the_role_a_deploy_needs(world: World) -> None:
    prod = f"{DEMO}/environments/production/resources"
    deploy = RULES[Action.DEPLOYMENT_REDEPLOY].minimum(None)
    limits = RULES[Action.CONTAINER_LIMITS]
    assert limits.minimum(EnvironmentName.PRODUCTION).rank >= deploy.rank
    assert _put_as(world["maintainer"], prod) == 200
    for who in ("viewer", "developer"):
        assert _put_as(world[who], prod) == 403
    # an environment grant on development gives nothing on production
    assert world["devonly"].get(prod).status_code == 404
    assert _put_as(world["devonly"], prod) == 404
    assert world.owner.put(f"{prod}/web", {"cpus": 1}).status_code == 200


def test_container_grants_never_allow_changing_limits(world: World) -> None:
    base = f"{DEMO}/environments/development/resources"
    for who in ("logs", "actions", "terminal"):
        assert _put_as(world[who], base) == 403
    # but they read the limits of the service they are granted, and only that one
    for who in ("logs", "actions", "terminal"):
        listed = world[who].get(base)
        assert listed.status_code == 200
        assert [item["service"] for item in listed.json()["services"]] == ["web"]
        stats = world[who].get(f"{base}/stats")
        assert stats.status_code == 200


def test_viewers_read_every_service(world: World) -> None:
    base = f"{DEMO}/environments/development/resources"
    listed = world["viewer"].get(base)
    assert [item["service"] for item in listed.json()["services"]] == ["web", "worker"]


def test_a_member_granted_on_another_project_sees_nothing_here(world: World) -> None:
    other = add_user(world.container, "other-owner")
    grant(world.container, "other", other, role=AccessRole.MAINTAINER)
    session = sign_in(world.container, "other-owner")
    assert session.get(BASE).status_code == 404
    assert session.put(f"{BASE}/web", {"cpus": 1}).status_code == 404
