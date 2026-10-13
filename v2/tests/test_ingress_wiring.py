"""The worker's real wiring: secret store token, Cloudflare client, ACME client and solvers.

Cloudflare and the CA are in-process fakes behind one ``httpx.MockTransport``; the host helper
is the in-memory fake. Nothing here can reach the network.
"""

import stat
from pathlib import Path

import httpx
import pytest
from sqlalchemy import Engine

from shiftcodes.adapters.acme.solvers import dns01_value
from shiftcodes.adapters.worker_client import WorkerClient
from shiftcodes.config import Settings
from shiftcodes.config_ingress import IngressSettings, get_ingress_settings
from shiftcodes.domain.actor import Actor
from shiftcodes.domain.enums import ActorType
from shiftcodes.domain.errors import ConfigurationError, NotFoundError
from shiftcodes.domain.ingress import CLOUDFLARE_TOKEN_SECRET, DomainStatus
from shiftcodes.logging import SecretRedactor
from shiftcodes.repositories.database import UnitOfWork, create_session_factory
from shiftcodes.services.audit import AuditService
from shiftcodes.services.domains import DomainDraft
from shiftcodes.services.secret_cipher import SecretCipher
from shiftcodes.services.secrets import SecretService
from shiftcodes.worker.__main__ import build_ingress, build_rpc_server
from shiftcodes.worker.ingress_wiring import IngressFactory
from tests.api_support import FakeClock
from tests.conftest import DEV
from tests.fake_acme import BASE, FakeAcmeCa
from tests.ingress_support import HOST, FakeCloudflare, FakeHelper
from tests.ingress_world import ACTOR

TOKEN = "cf-test-token"


class World:
    def __init__(self, tmp_path: Path, engine: Engine, **overrides: object) -> None:
        self.cloudflare = FakeCloudflare(TOKEN)
        self.ca = FakeAcmeCa(self._verify)
        self.ca.pending_polls = 0
        self.helper = FakeHelper()
        factory = create_session_factory(engine)
        self._session_factory = factory
        self.settings = Settings.model_validate({"data_dir": tmp_path / "data"})
        values: dict[str, object] = {
            "enabled": True,
            "public_ipv4": "203.0.113.7",
            "cloudflare_api_base": "https://api.test/client/v4",
            "acme_directory_url": f"{BASE}/directory",
            "acme_email": "ops@example.com",
            "dns_propagation_wait_seconds": 0,
        }
        values.update(overrides)
        self.ingress_settings = IngressSettings.model_validate(values)
        cipher = SecretCipher(tmp_path / "keys" / "secret.key")
        self.secrets = SecretService(self.uow(), cipher)
        self.factory = IngressFactory(
            self.settings,
            self.ingress_settings,
            factory,
            cipher,
            SecretRedactor(),
            transport=httpx.MockTransport(self._route),
        )
        self.factory.helper = self.helper

    def uow(self) -> UnitOfWork:
        return UnitOfWork(self._session_factory)

    def _route(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "ca.test":
            return self.ca.handle(request)
        return self.cloudflare.handle(request)

    def _verify(self, kind: str, domain: str, token: str, key_authorization: str) -> bool:
        if kind == "http-01":
            return self.helper.challenges.get(token) == key_authorization
        value = dns01_value(key_authorization)
        name = f"_acme-challenge.{domain}"
        return any(r["name"] == name and r["content"] == value for r in self.cloudflare.records)

    def set_token(self) -> None:
        self.secrets.set("demo", DEV, CLOUDFLARE_TOKEN_SECRET, TOKEN, Actor(ActorType.USER, "1"))


@pytest.fixture
def world(tmp_path: Path, engine: Engine, project: str) -> World:
    return World(tmp_path, engine)


def create_domain(world: World, hostname: str = HOST) -> int:
    draft = DomainDraft(hostname, "web", 8080, 20001)
    return world.factory.domains().create("demo", DEV, draft, ACTOR).id


def test_apply_uses_the_token_from_the_secret_store_and_real_acme_dns01(world: World) -> None:
    world.set_token()
    domain_id = create_domain(world)
    service = world.factory.ingress_service()
    plan = service.preview(domain_id)
    service.apply(domain_id, plan.plan_hash, ACTOR)
    # DNS record created with the token, TXT proof cleaned up, certificate and TLS vhost live.
    assert [(r["type"], r["name"]) for r in world.cloudflare.records] == [("A", HOST)]
    assert "SSLEngine on" in world.helper.vhosts[HOST]
    assert HOST in world.helper.certificates
    assert world.factory.domains().get_by_id(domain_id).status is DomainStatus.ACTIVE
    # The token read is audited by the secret store (as the worker), never logged elsewhere.
    actions = [e.action for e in AuditService(world.uow()).list(limit=50)]
    assert "secret.resolve" in actions and "certificate.issue" in actions
    # The ACME account key exists, private, in the worker-private directory.
    key_file = world.settings.data_dir / "worker-private" / "acme-account.key"
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert world.ca.new_account_calls == 1


def test_a_missing_cloudflare_secret_is_a_clear_not_found(world: World) -> None:
    domain_id = create_domain(world)
    with pytest.raises(NotFoundError, match=CLOUDFLARE_TOKEN_SECRET):
        world.factory.ingress_service().preview(domain_id)


def test_the_acme_email_is_required_for_issuance(
    tmp_path: Path, engine: Engine, project: str
) -> None:
    world = World(tmp_path, engine, acme_email=None)
    world.set_token()
    domain_id = create_domain(world)
    service = world.factory.ingress_service()
    plan = service.preview(domain_id)
    with pytest.raises(ConfigurationError, match="ACME_EMAIL"):
        service.apply(domain_id, plan.plan_hash, ACTOR)


def test_http01_is_pluggable_and_publishes_through_the_helper(
    tmp_path: Path, engine: Engine, project: str
) -> None:
    world = World(tmp_path, engine, acme_challenge="http-01")
    world.set_token()
    domain_id = create_domain(world)
    service = world.factory.ingress_service()
    plan = service.preview(domain_id)
    assert plan.tls.challenge == "http-01"
    service.apply(domain_id, plan.plan_hash, ACTOR)
    assert world.helper.challenges == {}  # removed again after validation
    assert HOST in world.helper.certificates


def test_check_and_renewal_services_are_built_from_the_same_wiring(world: World) -> None:
    assert world.factory.check_service() is not world.factory.check_service()
    scheduler = world.factory.renewal_scheduler()
    assert scheduler.tick() == []  # nothing to renew yet


def test_ingress_is_off_unless_enabled(
    tmp_path: Path, engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings.model_validate(
        {"data_dir": tmp_path / "d", "secret_key_file": tmp_path / "k"}
    )
    factory = create_session_factory(engine)
    monkeypatch.delenv("SHIFTCODES_INGRESS_ENABLED", raising=False)
    get_ingress_settings.cache_clear()
    assert build_ingress(settings, factory, SecretRedactor()) is None
    monkeypatch.setenv("SHIFTCODES_INGRESS_ENABLED", "true")
    get_ingress_settings.cache_clear()
    try:
        enabled = build_ingress(settings, factory, SecretRedactor())
        assert isinstance(enabled, IngressFactory)
        enabled.close()
    finally:
        get_ingress_settings.cache_clear()


def test_extra_handlers_extend_the_rpc_table(
    tmp_path: Path, engine: Engine, socket_dir: Path, clock: FakeClock
) -> None:
    settings = Settings.model_validate(
        {
            "data_dir": tmp_path / "d",
            "worker_socket_path": socket_dir / "w.sock",
            "secret_key_file": tmp_path / "keys" / "secret.key",
        }
    )
    server = build_rpc_server(
        settings,
        create_session_factory(engine),
        SecretRedactor(),
        {"extra.ping": lambda _c: "pong"},
    )
    server.start()
    try:
        client = WorkerClient(settings.worker_socket_path)
        client.ping()
        assert client._call("extra.ping", None, {}) == "pong"
    finally:
        server.stop()
