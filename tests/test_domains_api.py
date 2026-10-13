"""The domain endpoints end to end: API -> worker RPC -> services with fake Cloudflare/Apache/CA."""

from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from backend.adapters.host_helper_client import HostHelperError
from backend.adapters.ingress_client import IngressClient
from backend.repositories.database import UnitOfWork, create_session_factory
from backend.services.audit import AuditService
from backend.services.domain_checks import CheckProbes, DomainCheckService
from backend.worker.ingress_handlers import build_ingress_handlers
from backend.worker.rpc import RpcServer
from tests.api_support import (
    FakeClock,
    create_user,
    login,
    make_client,
    make_container,
    make_settings,
    mutation_headers,
)
from tests.ingress_support import HOST
from tests.ingress_world import DEFAULT_IPV4, IngressWorld
from tests.test_domain_checks import GOOD, FakeProber, FakeResolver

BASE = "/api/projects/demo/environments/development/domains"
PROD_BASE = "/api/projects/demo/environments/production/domains"
BODY = {"hostname": HOST, "service": "web", "container_port": 8080, "published_port": 20001}


@dataclass
class Api:
    client: TestClient
    csrf: str
    world: IngressWorld
    engine: Engine

    def headers(self) -> dict[str, str]:
        return mutation_headers(self.client, self.csrf)

    def post(self, url: str, body: dict[str, object] | None = None) -> object:
        return self.client.post(url, json=body, headers=self.headers())

    def create(self, **overrides: object) -> int:
        response = self.client.post(BASE, json={**BODY, **overrides}, headers=self.headers())
        assert response.status_code == 201, response.text
        return int(response.json()["id"])

    def preview(self, domain_id: int, operation: str = "apply") -> dict[str, object]:
        response = self.client.post(
            f"{BASE}/{domain_id}/preview", json={"operation": operation}, headers=self.headers()
        )
        assert response.status_code == 200, response.text
        return dict(response.json())

    def apply(self, domain_id: int) -> object:
        plan = self.preview(domain_id)
        return self.client.post(
            f"{BASE}/{domain_id}/apply",
            json={"plan_hash": plan["plan_hash"]},
            headers=self.headers(),
        )


@pytest.fixture
def api(
    tmp_path: Path, socket_dir: Path, engine: Engine, clock: FakeClock, project: str
) -> Iterator[Api]:
    factory = create_session_factory(engine)
    world = IngressWorld(lambda: UnitOfWork(factory), clock)
    probes = CheckProbes(FakeResolver({HOST: [DEFAULT_IPV4]}), FakeProber(GOOD), world.helper)

    def checks() -> DomainCheckService:
        return DomainCheckService(UnitOfWork(factory), probes, world.settings, clock)

    socket_path = socket_dir / "w.sock"
    server = RpcServer(socket_path, build_ingress_handlers(world.ingress, checks))
    server.start()
    try:
        settings = make_settings(tmp_path, socket_path=socket_path)
        container = replace(
            make_container(settings, engine, clock), ingress=IngressClient(socket_path)
        )
        create_user(container)
        client = make_client(container)
        yield Api(client, login(client), world, engine)
    finally:
        server.stop()


def bearer(api: Api, scopes: list[str]) -> dict[str, str]:
    response = api.client.post(
        "/api/tokens", json={"name": "agent", "scopes": scopes}, headers=api.headers()
    )
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_create_list_get_update_delete(api: Api) -> None:
    domain_id = api.create()
    listing = api.client.get(BASE)
    [item] = listing.json()
    assert item["hostname"] == HOST and item["status"] == "draft"
    assert item["certificate"] is None and item["check"] is None
    assert api.client.get(PROD_BASE).json() == []
    detail = api.client.get(f"{BASE}/{domain_id}")
    assert detail.status_code == 200 and detail.json()["published_port"] == 20001
    patched = api.client.patch(
        f"{BASE}/{domain_id}",
        json={"published_port": 20009, "dns_proxied": True},
        headers=api.headers(),
    )
    assert patched.status_code == 200
    assert (patched.json()["published_port"], patched.json()["dns_proxied"]) == (20009, True)
    assert api.client.delete(f"{BASE}/{domain_id}", headers=api.headers()).status_code == 204
    assert api.client.get(f"{BASE}/{domain_id}").status_code == 404


def test_validation_and_uniqueness_errors(api: Api) -> None:
    api.create()
    duplicate = api.client.post(BASE, json=BODY, headers=api.headers())
    assert duplicate.status_code == 409 and duplicate.json()["error"]["code"] == "conflict"
    other_env = api.client.post(PROD_BASE, json=BODY, headers=api.headers())
    assert other_env.status_code == 409  # unique across environments too
    for bad in (
        {**BODY, "hostname": "not a host", "published_port": 20002},
        {**BODY, "hostname": "*.example.com"},
        {**BODY, "hostname": "b.example.com", "published_port": 80},
        {**BODY, "hostname": "b.example.com", "unknown": 1},
        {**BODY, "hostname": "b.example.com", "dns_record_type": "MX"},
    ):
        response = api.client.post(BASE, json=bad, headers=api.headers())
        assert response.status_code == 422, bad
    assert (
        api.client.get("/api/projects/missing/environments/development/domains").status_code == 404
    )


def test_preview_then_apply_flow(api: Api) -> None:
    domain_id = api.create()
    plan = api.preview(domain_id)
    assert plan["blocked"] is False and plan["operation"] == "apply"
    assert plan["dns"]["action"] == "create" and plan["dns"]["desired"]["content"] == DEFAULT_IPV4  # type: ignore[index]
    assert plan["vhost"]["action"] == "create" and "+<VirtualHost *:80>" in plan["vhost"]["diff"]  # type: ignore[index]
    assert plan["tls"]["action"] == "issue"  # type: ignore[index]
    assert (
        api.world.cloudflare.mutations() == [] and api.world.helper.vhosts == {}
    )  # preview is dry

    response = api.client.post(
        f"{BASE}/{domain_id}/apply", json={"plan_hash": plan["plan_hash"]}, headers=api.headers()
    )
    assert response.status_code == 200, response.text
    assert HOST in api.world.helper.vhosts and api.world.cloudflare.records
    detail = api.client.get(f"{BASE}/{domain_id}").json()
    assert detail["status"] == "active" and detail["certificate"]["state"] == "ok"
    assert detail["certificate"]["days_remaining"] == 90

    stale = api.client.post(
        f"{BASE}/{domain_id}/apply", json={"plan_hash": "f" * 64}, headers=api.headers()
    )
    assert (
        stale.status_code == 409 and "changed since the preview" in stale.json()["error"]["message"]
    )
    malformed = api.client.post(
        f"{BASE}/{domain_id}/apply", json={"plan_hash": "short"}, headers=api.headers()
    )
    assert malformed.status_code == 422


def test_a_foreign_dns_record_blocks_apply_with_a_clear_conflict(api: Api) -> None:
    domain_id = api.create()
    api.world.cloudflare.add_record(type="A", name=HOST, content="192.0.2.9", comment="manual")
    plan = api.preview(domain_id)
    assert plan["blocked"] is True and plan["dns"]["action"] == "conflict"  # type: ignore[index]
    response = api.client.post(
        f"{BASE}/{domain_id}/apply", json={"plan_hash": plan["plan_hash"]}, headers=api.headers()
    )
    assert response.status_code == 409
    assert "not managed by Jendock" in response.json()["error"]["message"]
    assert api.world.cloudflare.mutations() == []


def test_an_apache_failure_is_reported_as_upstream_error_and_marks_the_domain(api: Api) -> None:
    domain_id = api.create(tls_enabled=False)
    api.world.helper.fail_put = HostHelperError(
        "configtest_failed", "configtest failed, change reverted"
    )
    response = api.apply(domain_id)
    assert response.status_code == 502  # type: ignore[attr-defined]
    error = response.json()["error"]  # type: ignore[attr-defined]
    assert error["code"] == "upstream_error" and "change reverted" in error["message"]
    detail = api.client.get(f"{BASE}/{domain_id}").json()
    assert detail["status"] == "error" and "change reverted" in detail["status_message"]


def test_check_certificate_and_overview_endpoints(api: Api) -> None:
    domain_id = api.create()
    assert api.apply(domain_id).status_code == 200  # type: ignore[attr-defined]
    check = api.post(f"{BASE}/{domain_id}/check")
    assert check.status_code == 200  # type: ignore[attr-defined]
    body = check.json()  # type: ignore[attr-defined]
    assert body["ok"] is True and set(body["results"]) == {"dns", "https", "backend"}
    detail = api.client.get(f"{BASE}/{domain_id}").json()
    assert detail["check"]["checked_at"] == body["checked_at"]
    assert detail["last_verified_ok"] is True
    issued = api.post(f"{BASE}/{domain_id}/certificate")
    assert issued.status_code == 200 and issued.json()["state"] == "ok"  # type: ignore[attr-defined]
    overview = api.client.get("/api/ingress/certificates").json()
    assert [(c["hostname"], c["state"]) for c in overview] == [(HOST, "ok")]


def test_remove_then_delete(api: Api) -> None:
    domain_id = api.create()
    api.apply(domain_id)
    assert api.client.delete(f"{BASE}/{domain_id}", headers=api.headers()).status_code == 409
    plan = api.preview(domain_id, "remove")
    assert plan["operation"] == "remove" and plan["dns"]["action"] == "delete"  # type: ignore[index]
    removed = api.post(f"{BASE}/{domain_id}/remove", {"plan_hash": plan["plan_hash"]})
    assert removed.status_code == 200  # type: ignore[attr-defined]
    assert api.world.helper.vhosts == {} and api.world.cloudflare.records == []
    assert api.client.get(f"{BASE}/{domain_id}").json()["status"] == "draft"
    assert api.client.delete(f"{BASE}/{domain_id}", headers=api.headers()).status_code == 204


def test_scopes_read_for_status_admin_for_everything_else(api: Api) -> None:
    domain_id = api.create()
    reader = bearer(api, ["read"])
    runner = bearer(api, ["run", "deploy"])
    admin = bearer(api, ["admin"])
    anonymous = TestClient(api.client.app, base_url=str(api.client.base_url))
    assert anonymous.get(BASE).status_code == 401
    assert api.client.get(BASE, headers=reader).status_code == 200
    assert api.client.get(f"{BASE}/{domain_id}", headers=reader).status_code == 200
    assert api.client.get("/api/ingress/certificates", headers=reader).status_code == 200
    assert api.client.get(BASE, headers=runner).status_code == 403
    mutations = [
        ("post", BASE, BODY),
        ("patch", f"{BASE}/{domain_id}", {"dns_proxied": True}),
        ("delete", f"{BASE}/{domain_id}", None),
        ("post", f"{BASE}/{domain_id}/preview", {}),
        ("post", f"{BASE}/{domain_id}/apply", {"plan_hash": "0" * 64}),
        ("post", f"{BASE}/{domain_id}/remove", {"plan_hash": "0" * 64}),
        ("post", f"{BASE}/{domain_id}/check", None),
        ("post", f"{BASE}/{domain_id}/certificate", None),
    ]
    for method, url, body in mutations:
        for headers in (reader, runner):
            response = api.client.request(method, url, json=body, headers=headers)
            assert response.status_code == 403, (method, url)
    assert api.client.post(f"{BASE}/{domain_id}/preview", json={}, headers=admin).status_code == 200


def test_session_mutations_need_csrf_and_origin(api: Api) -> None:
    assert api.client.post(BASE, json=BODY).status_code == 403
    wrong = {"Origin": "https://evil.example", "X-CSRF-Token": api.csrf}
    assert api.client.post(BASE, json=BODY, headers=wrong).status_code == 403
    assert api.client.get(BASE).json() == []


def test_every_mutation_is_audited_with_the_acting_token(api: Api, engine: Engine) -> None:
    admin = bearer(api, ["admin"])
    created = api.client.post(BASE, json=BODY, headers=admin)
    domain_id = created.json()["id"]
    plan = api.client.post(f"{BASE}/{domain_id}/preview", json={}, headers=admin).json()
    api.client.post(
        f"{BASE}/{domain_id}/apply", json={"plan_hash": plan["plan_hash"]}, headers=admin
    )
    events = AuditService(UnitOfWork(create_session_factory(engine))).list(limit=50)
    by_action = {e.action: e for e in events}
    assert {"domain.create", "domain.apply", "certificate.issue"} <= set(by_action)
    assert by_action["domain.create"].actor_type.value == "token"
    assert by_action["domain.apply"].actor_type.value == "token"
    assert by_action["domain.apply"].details["hostname"] == HOST
    assert "token" not in repr([e.details for e in events]).lower().replace("actor", "")


def test_the_cloudflare_token_never_appears_in_responses(api: Api) -> None:
    domain_id = api.create()
    api.world.cloudflare.reject_auth = True
    response = api.client.post(f"{BASE}/{domain_id}/preview", json={}, headers=api.headers())
    assert response.status_code == 502
    assert api.world.cloudflare.token not in response.text


def test_ingress_calls_report_the_worker_as_unavailable_when_not_wired(
    tmp_path: Path, engine: Engine, clock: FakeClock, project: str
) -> None:
    container = make_container(make_settings(tmp_path), engine, clock)
    create_user(container)
    client = make_client(container)
    csrf = login(client)
    headers = mutation_headers(client, csrf)
    created = client.post(BASE, json=BODY, headers=headers)
    assert created.status_code == 201  # definition works without the worker
    response = client.post(f"{BASE}/{created.json()['id']}/preview", json={}, headers=headers)
    assert response.status_code == 503
