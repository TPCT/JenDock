"""Settings -> Integrations: the Cloudflare credential, test connection, settings, GitHub status.

The API talks to a real worker RPC server (real cipher, services and handlers); Cloudflare is an
in-process ``httpx.MockTransport``. The central claim under test: the token is write-only. It is
never returned by an endpoint, never written to an audit record, never logged.
"""

from __future__ import annotations

import dataclasses
import logging
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select

from backend.adapters.integrations_client import IntegrationsClient
from backend.api.container import AppContainer
from backend.config import Settings
from backend.config_ingress import IngressSettings
from backend.domain.actor import Actor
from backend.domain.enums import TokenScope
from backend.domain.ingress import CLOUDFLARE_TOKEN_SECRET
from backend.logging import SecretRedactor
from backend.mcp.tools import default_registry
from backend.repositories.database import UnitOfWork, create_session_factory
from backend.repositories.models import AuditEvent, PlatformSecret, WebhookDelivery
from backend.services.audit import AuditService
from backend.services.platform_secrets import CLOUDFLARE_TOKEN, PlatformSecretService
from backend.services.secret_cipher import SecretCipher
from backend.services.secrets import SecretService
from backend.worker.__main__ import build_rpc_server
from backend.worker.integrations_wiring import build_integrations
from tests.api_support import (
    FakeClock,
    create_user,
    login,
    make_client,
    make_container,
    mutation_headers,
)
from tests.conftest import DEV, SLUG

TOKEN = "cf-secret-token-0123456789abcdef"
needs_keygen = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="no ssh-keygen")


def cloudflare_handler(request: httpx.Request) -> httpx.Response:
    """Verify and zones endpoints of a fake Cloudflare that accepts exactly ``TOKEN``."""
    if request.headers.get("authorization") != f"Bearer {TOKEN}":
        return httpx.Response(403, json={"success": False, "errors": [{"code": 9109}]})
    path = request.url.path
    if path.endswith("/user/tokens/verify"):
        return httpx.Response(
            200, json={"success": True, "result": {"id": "t", "status": "active"}}
        )
    if path.endswith("/zones"):
        zones = [{"id": "z2", "name": "zeta.example"}, {"id": "z1", "name": "alpha.example"}]
        return httpx.Response(
            200, json={"success": True, "result": zones, "result_info": {"total_pages": 1}}
        )
    return httpx.Response(404, json={"success": False, "errors": [{"message": "no"}]})


@dataclasses.dataclass
class World:
    container: AppContainer
    client: TestClient
    csrf: str
    engine: Engine
    key_dir: Path

    def put(self, url: str, body: dict[str, object]) -> httpx2.Response:
        return self.client.put(url, json=body, headers=mutation_headers(self.client, self.csrf))

    def post(self, url: str) -> httpx2.Response:
        return self.client.post(url, headers=mutation_headers(self.client, self.csrf))


@contextmanager
def make_world(
    tmp_path: Path, engine: Engine, socket_dir: Path, clock: FakeClock, **env: object
) -> Iterator[World]:
    key_dir = tmp_path / "deploy_keys"
    settings = Settings.model_validate(
        {
            "data_dir": tmp_path / "data",
            "public_origin": "https://app.example.test",
            "worker_socket_path": socket_dir / "w.sock",
            "secret_key_file": tmp_path / "keys" / "secret.key",
            "git_ssh_key_dir": key_dir,
            "github_webhook_secret_file": tmp_path / "webhook.secret",
        }
    )
    (tmp_path / "webhook.secret").write_text("whsec")
    ingress = IngressSettings.model_validate(
        {"enabled": True, "helper_socket_path": socket_dir / "none.sock", **env}
    )
    factory = create_session_factory(engine)
    cipher = SecretCipher(settings.secret_key_file)
    redactor = SecretRedactor()
    integrations = build_integrations(
        settings,
        ingress,
        factory,
        cipher,
        redactor,
        transport=httpx.MockTransport(cloudflare_handler),
    )
    server = build_rpc_server(settings, factory, redactor, integrations)
    server.start()
    try:
        container = dataclasses.replace(
            make_container(settings, engine, clock),
            integrations=IntegrationsClient(settings.worker_socket_path),
            ingress_settings=ingress,
        )
        client = make_client(container)
        create_user(container)
        yield World(container, client, login(client), engine, key_dir)
    finally:
        server.stop()


@pytest.fixture
def world(
    tmp_path: Path, engine: Engine, socket_dir: Path, clock: FakeClock, project: str
) -> Iterator[World]:
    with make_world(tmp_path, engine, socket_dir, clock) as built:
        yield built


def everything_stored(engine: Engine) -> str:
    """Every audit record, as text."""
    uow = UnitOfWork(create_session_factory(engine))
    with uow:
        rows = uow.session.scalars(select(AuditEvent)).all()
        return repr([(r.action, r.target_type, r.target_id, r.details) for r in rows])


def test_the_token_is_encrypted_at_rest_and_never_returned_logged_or_audited(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    response = world.put("/api/integrations/cloudflare/token", {"token": TOKEN})
    assert response.status_code == 200
    seen = [response.text]
    seen.append(world.client.get("/api/integrations/cloudflare").text)
    seen.append(world.post("/api/integrations/cloudflare/test").text)
    seen.append(world.client.get("/api/integrations/github").text)
    seen.append(world.client.get("/api/ingress/readiness").text)
    seen.append(world.client.get("/api/tokens").text)
    assert all(TOKEN not in text for text in seen)
    assert world.client.get("/api/integrations/cloudflare").json()["token_configured"] is True
    assert TOKEN not in everything_stored(world.engine)
    assert TOKEN not in caplog.text
    uow = UnitOfWork(create_session_factory(world.engine))
    with uow:
        stored = uow.session.scalars(select(PlatformSecret)).one()
        assert stored.name == CLOUDFLARE_TOKEN
        assert TOKEN.encode() not in stored.ciphertext
    actions = [e.action for e in AuditService(world.container.uow()).list(limit=50)]
    assert "integration.credential_set" in actions
    assert "integration.cloudflare_test" in actions


def test_test_connection_verifies_with_cloudflare_and_lists_zone_names_only(world: World) -> None:
    world.put("/api/integrations/cloudflare/token", {"token": TOKEN})
    result = world.post("/api/integrations/cloudflare/test").json()
    assert result == {
        "ok": True,
        "status": "active",
        "zones": ["alpha.example", "zeta.example"],
        "message": "",
    }


def test_a_wrong_token_fails_the_test_without_an_http_error(world: World) -> None:
    world.put("/api/integrations/cloudflare/token", {"token": "x" * 40})
    result = world.post("/api/integrations/cloudflare/test").json()
    assert result["ok"] is False and result["zones"] == []
    assert "rejected" in result["message"]


def test_testing_without_a_token_says_so(world: World) -> None:
    result = world.post("/api/integrations/cloudflare/test").json()
    assert result["ok"] is False and "no token" in result["message"]


def test_clearing_forgets_the_token(world: World) -> None:
    world.put("/api/integrations/cloudflare/token", {"token": TOKEN})
    response = world.client.delete(
        "/api/integrations/cloudflare/token", headers=mutation_headers(world.client, world.csrf)
    )
    assert response.status_code == 200 and response.json()["token_configured"] is False
    reasons = {r["code"] for r in response.json()["reasons"]}
    assert "no_token" in reasons


def test_a_malformed_token_is_rejected_without_echoing_it(world: World) -> None:
    bad = "has a space " + TOKEN
    response = world.put("/api/integrations/cloudflare/token", {"token": bad})
    assert response.status_code == 422
    assert TOKEN not in response.text
    assert world.put("/api/integrations/cloudflare/token", {"token": ""}).status_code == 422


def test_status_explains_why_ingress_cannot_work(world: World) -> None:
    body = world.client.get("/api/integrations/cloudflare").json()
    assert body["ready"] is False
    assert body["helper"]["enabled"] is True and body["helper"]["reachable"] is False
    assert "does not exist" in body["helper"]["reason"]
    codes = {r["code"] for r in body["reasons"]}
    assert {"helper_unreachable", "no_token", "no_acme_email"} <= codes
    advisory = next(r for r in body["reasons"] if r["code"] == "no_acme_email")
    assert advisory["blocking"] is False


def test_ingress_disabled_is_reported_with_the_overlay_hint(
    tmp_path: Path, engine: Engine, socket_dir: Path, clock: FakeClock, project: str
) -> None:
    with make_world(tmp_path, engine, socket_dir, clock, enabled=False) as world:
        body = world.client.get("/api/integrations/cloudflare").json()
        message = next(r["message"] for r in body["reasons"] if r["code"] == "ingress_disabled")
        assert "compose.ingress.yml" in message
        readiness = world.client.get("/api/ingress/readiness").json()
        assert readiness["ready"] is False


def test_settings_are_stored_validated_audited_and_override_the_environment(
    tmp_path: Path, engine: Engine, socket_dir: Path, clock: FakeClock, project: str
) -> None:
    with make_world(tmp_path, engine, socket_dir, clock, public_ipv4="203.0.113.1") as world:
        url = "/api/integrations/cloudflare/settings"
        ok = world.put(
            url,
            {
                "acme_email": "ops@example.com",
                "public_ipv6": "2001:db8::1",
                "acme_challenge": "http-01",
            },
        )
        assert ok.status_code == 200
        body = ok.json()
        assert body["stored"]["acme_email"] == "ops@example.com"
        assert body["effective"]["public_ipv4"] == "203.0.113.1"  # environment default
        assert body["defaults"]["public_ipv4"] == "203.0.113.1"
        assert body["effective"]["acme_challenge"] == "http-01"
        assert world.put(url, {"public_ipv4": "not-an-ip"}).status_code == 422
        assert world.put(url, {"acme_email": "nope"}).status_code == 422
        assert world.put(url, {"acme_challenge": "tls-alpn"}).status_code == 422
        reset = world.put(url, {"acme_email": None}).json()
        assert reset["stored"]["acme_email"] is None
        assert reset["stored"]["public_ipv6"] == "2001:db8::1"  # absent fields stay
        audited = [
            e
            for e in AuditService(world.container.uow()).list(limit=20)
            if e.action == "integration.ingress_settings_update"
        ]
        assert len(audited) == 2
        assert "ops@example.com" not in everything_stored(engine)


def test_integrations_are_for_browser_sessions_and_need_csrf(
    world: World, container_token: str
) -> None:
    bearer = {"Authorization": f"Bearer {container_token}"}
    bare = TestClient(world.client.app, base_url=str(world.client.base_url))
    for method, url in [
        ("GET", "/api/integrations/cloudflare"),
        ("GET", "/api/integrations/github"),
        ("POST", "/api/integrations/cloudflare/test"),
        ("GET", "/api/mcp/tools"),
    ]:
        assert bare.request(method, url, headers=bearer).status_code == 403, url
        assert bare.request(method, url).status_code == 401, url
    no_csrf = world.client.put(
        "/api/integrations/cloudflare/token",
        json={"token": TOKEN},
        headers={"Origin": "https://app.example.test"},
    )
    assert no_csrf.status_code == 403


@pytest.fixture
def container_token(world: World) -> str:
    response = world.client.post(
        "/api/tokens",
        json={"name": "agent", "scopes": ["admin"]},
        headers=mutation_headers(world.client, world.csrf),
    )
    return str(response.json()["token"])


def test_readiness_is_readable_with_a_read_token(world: World, container_token: str) -> None:
    bare = TestClient(world.client.app, base_url=str(world.client.base_url))
    response = bare.get(
        "/api/ingress/readiness", headers={"Authorization": f"Bearer {container_token}"}
    )
    assert response.status_code == 200 and "reasons" in response.json()


def test_an_environment_secret_still_counts_as_a_token_for_readiness(world: World) -> None:
    cipher = SecretCipher(world.container.settings.secret_key_file)
    SecretService(world.container.uow(), cipher).set(
        SLUG, DEV, CLOUDFLARE_TOKEN_SECRET, TOKEN, Actor.system("test")
    )
    plain = world.client.get("/api/ingress/readiness").json()
    assert plain["token_configured"] is False
    scoped = world.client.get(
        f"/api/ingress/readiness?project={SLUG}&environment=development"
    ).json()
    assert scoped["token_configured"] is True


def test_github_overview_has_webhook_status_and_a_delivery_summary(
    world: World, clock: FakeClock
) -> None:
    uow = world.container.uow()
    with uow:
        for number, outcome in enumerate(["queued", "queued", "ignored"]):
            uow.deliveries.add(
                WebhookDelivery(
                    delivery_id=f"d{number}",
                    event="push",
                    repository="octo/demo",
                    outcome=outcome,
                    received_at=clock.now,
                )
            )
    body = world.client.get("/api/integrations/github").json()
    assert body["webhook"]["payload_url"] == "https://app.example.test/api/github/webhook"
    assert body["webhook"]["secret_configured"] is True
    assert body["webhook"]["counts_7d"] == {"queued": 2, "ignored": 1}
    assert len(body["webhook"]["recent"]) == 3
    assert "whsec" not in str(body)
    key = body["deploy_keys"][0]
    assert key["slug"] == SLUG and key["exists"] is False and key["public_key"] is None


@needs_keygen
def test_deploy_key_is_created_by_the_worker_and_only_the_public_key_is_returned(
    world: World,
) -> None:
    created = world.post(f"/api/integrations/github/projects/{SLUG}/deploy-key")
    assert created.status_code == 201
    public = created.json()["public_key"]
    assert public.startswith("ssh-ed25519 ")
    private = (world.key_dir / "octo__demo").read_text()
    assert "PRIVATE KEY" in private
    seen = world.client.get("/api/integrations/github").text + created.text
    assert "PRIVATE KEY" not in seen
    assert private.splitlines()[1] not in seen
    listed = world.client.get("/api/integrations/github").json()["deploy_keys"][0]
    assert listed["exists"] is True and listed["public_key"] == public
    again = world.post(f"/api/integrations/github/projects/{SLUG}/deploy-key")
    assert again.status_code == 409
    actions = [e.action for e in AuditService(world.container.uow()).list(limit=20)]
    assert "deploy_key.create" in actions
    assert "PRIVATE KEY" not in everything_stored(world.engine)


def test_deploy_key_for_an_unknown_project_is_404(world: World) -> None:
    assert world.post("/api/integrations/github/projects/nope/deploy-key").status_code == 404


def test_the_platform_secret_service_round_trips_and_audits_names_only(
    engine: Engine, cipher: SecretCipher
) -> None:
    redactor = SecretRedactor()
    service = PlatformSecretService(UnitOfWork(create_session_factory(engine)), cipher, redactor)
    service.set(CLOUDFLARE_TOKEN, f"  {TOKEN}  ", Actor.system("t"))
    assert service.resolve(CLOUDFLARE_TOKEN) == TOKEN
    assert TOKEN not in everything_stored(engine)
    service.clear(CLOUDFLARE_TOKEN, Actor.system("t"))
    assert service.resolve(CLOUDFLARE_TOKEN) is None


# ---- MCP tools endpoint ---------------------------------------------------------------------


def test_mcp_tools_are_derived_from_the_registry_for_the_selected_scopes(world: World) -> None:
    registry = default_registry()
    read = world.client.get("/api/mcp/tools?scopes=read").json()
    assert read["endpoint"] == "https://app.example.test/mcp"
    names = {t["name"] for t in read["tools"]}
    assert names == {t.name for t in registry.all() if t.scope is TokenScope.READ}
    run = world.client.get("/api/mcp/tools?scopes=read,run").json()
    expected = {t.name for t in registry.all() if t.scope in {TokenScope.READ, TokenScope.RUN}}
    assert {t["name"] for t in run["tools"]} == expected and len(expected) > len(names)
    admin = world.client.get("/api/mcp/tools?scopes=admin").json()
    assert {t["name"] for t in admin["tools"]} == {t.name for t in registry.all()}
    assert world.client.get("/api/mcp/tools").json()["tools"] == []
    assert world.client.get("/api/mcp/tools?scopes=root").status_code == 422


def test_mcp_never_offers_secret_values_or_a_terminal(world: World) -> None:
    admin = world.client.get("/api/mcp/tools?scopes=admin").json()["tools"]
    names = {t["name"] for t in admin}
    assert "list_secret_names" in names
    assert not any("reveal" in n or "terminal" in n or "shell" in n or "exec" in n for n in names)
