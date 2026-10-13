import pytest
from sqlalchemy import Engine

from backend.adapters.acme.client import AcmeError
from backend.adapters.host_helper_client import HostHelperError
from backend.domain.errors import ConfigurationError, ConflictError, UpstreamError
from backend.domain.ingress import MANAGED_TAG, DnsRecordType, DomainStatus, PlanAction
from backend.repositories.database import UnitOfWork, create_session_factory
from backend.services.audit import AuditService
from backend.services.domains import DomainPatch
from tests.api_support import FakeClock
from tests.conftest import DEV
from tests.ingress_support import HOST
from tests.ingress_world import ACTOR, DEFAULT_IPV4, IngressWorld


@pytest.fixture
def world(engine: Engine, clock: FakeClock, project: str) -> IngressWorld:
    factory = create_session_factory(engine)
    return IngressWorld(lambda: UnitOfWork(factory), clock)


def actions(world: IngressWorld) -> list[str]:
    return [e.action for e in AuditService(world.uow_factory()).list(limit=200)]


def test_preview_changes_nothing_and_describes_dns_vhost_and_tls(world: IngressWorld) -> None:
    domain = world.create()
    plan = world.ingress().preview(domain.id)
    assert plan.operation == "apply" and not plan.blocked
    assert plan.dns.action is PlanAction.CREATE
    assert plan.dns.zone == "example.com"
    assert plan.dns.desired is not None
    assert (plan.dns.desired.record_type, plan.dns.desired.content) == ("A", DEFAULT_IPV4)
    assert plan.vhost.action is PlanAction.CREATE
    assert "+<VirtualHost *:443>" in plan.vhost.diff
    assert "ProxyPass / http://127.0.0.1:20001/" in (plan.vhost.proposed or "")
    assert plan.tls.action == "issue" and plan.tls.challenge == "dns-01"
    assert len(plan.plan_hash) == 64
    # Nothing was touched anywhere.
    assert world.cloudflare.mutations() == []
    assert world.helper.vhosts == {} and world.helper.calls == []
    assert world.domains().get_by_id(domain.id).status is DomainStatus.DRAFT


def test_apply_rolls_out_dns_http_vhost_certificate_and_tls_vhost_in_order(
    world: IngressWorld,
) -> None:
    domain = world.create()
    plan = world.apply(domain)
    assert world.helper.calls == [
        "put_vhost tls=False",
        "install_certificate",
        "put_vhost tls=True",
    ]
    record = world.cloudflare.records[0]
    assert (record["type"], record["name"], record["content"]) == ("A", HOST, DEFAULT_IPV4)
    assert record["comment"] == f"{MANAGED_TAG} domain={domain.id}"
    assert "SSLEngine on" in world.helper.vhosts[HOST]
    stored = world.domains().get_by_id(domain.id)
    assert stored.status is DomainStatus.ACTIVE
    assert stored.cloudflare_record_id == record["id"]
    assert stored.cloudflare_zone_id == "023e105f4ecef8ad9ca31a8372d0c353"
    assert stored.applied_at is not None
    assert world.certificates().status_for(domain.id) is not None
    assert plan.plan_hash
    assert {"domain.create", "domain.apply", "certificate.issue"} <= set(actions(world))


def test_apply_requires_the_hash_of_the_reviewed_plan(world: IngressWorld) -> None:
    domain = world.create()
    plan = world.ingress().preview(domain.id)
    with pytest.raises(ConflictError, match="changed since the preview"):
        world.ingress().apply(domain.id, "0" * 64, ACTOR)
    assert world.cloudflare.mutations() == [] and world.helper.calls == []
    # The routing changes after the preview: the old hash must no longer be accepted.
    world.domains().update("demo", DEV, domain.id, DomainPatch(published_port=20002), ACTOR)
    with pytest.raises(ConflictError, match="changed since the preview"):
        world.ingress().apply(domain.id, plan.plan_hash, ACTOR)
    assert world.helper.calls == []


def test_a_second_apply_is_idempotent_and_reuses_the_certificate(world: IngressWorld) -> None:
    domain = world.create()
    world.apply(domain)
    world.cloudflare.requests.clear()
    world.helper.calls.clear()
    plan = world.apply(domain)
    assert plan.dns.action is PlanAction.NOOP
    assert plan.vhost.action is PlanAction.NOOP
    assert plan.tls.action == "keep"
    assert world.cloudflare.mutations() == []
    assert world.issuer.issued == [HOST]  # no second issuance
    assert "install_certificate" not in world.helper.calls


def test_changing_the_port_updates_only_the_vhost(world: IngressWorld) -> None:
    domain = world.create()
    world.apply(domain)
    world.domains().update("demo", DEV, domain.id, DomainPatch(published_port=20002), ACTOR)
    plan = world.ingress().preview(domain.id)
    assert (plan.dns.action, plan.vhost.action) == (PlanAction.NOOP, PlanAction.UPDATE)
    assert "-    ProxyPass / http://127.0.0.1:20001/ retry=0" in plan.vhost.diff
    assert "+    ProxyPass / http://127.0.0.1:20002/ retry=0" in plan.vhost.diff
    world.ingress().apply(domain.id, plan.plan_hash, ACTOR)
    assert "127.0.0.1:20002" in world.helper.vhosts[HOST]


def test_a_foreign_dns_record_blocks_the_plan_and_nothing_is_changed(world: IngressWorld) -> None:
    domain = world.create()
    foreign = world.cloudflare.add_record(
        type="A", name=HOST, content="192.0.2.99", comment="manual"
    )
    plan = world.ingress().preview(domain.id)
    assert plan.blocked and plan.dns.action is PlanAction.CONFLICT
    with pytest.raises(ConflictError, match="not managed by Jendock"):
        world.ingress().apply(domain.id, plan.plan_hash, ACTOR)
    assert world.cloudflare.records == [foreign]
    assert world.cloudflare.mutations() == []
    assert world.helper.calls == []
    assert world.domains().get_by_id(domain.id).status is DomainStatus.DRAFT


def test_a_failing_apache_change_marks_the_domain_failed_and_is_reported(
    world: IngressWorld,
) -> None:
    domain = world.create(tls_enabled=False)
    world.helper.fail_put = HostHelperError(
        "configtest_failed", "configtest failed, change reverted"
    )
    plan = world.ingress().preview(domain.id)
    with pytest.raises(UpstreamError, match="change reverted"):
        world.ingress().apply(domain.id, plan.plan_hash, ACTOR)
    stored = world.domains().get_by_id(domain.id)
    assert stored.status is DomainStatus.ERROR
    assert "change reverted" in (stored.status_message or "")
    assert "domain.apply_failed" in actions(world)
    # Fixing the cause and applying again converges (DNS record already exists, no duplicate).
    world.helper.fail_put = None
    world.apply(domain)
    assert world.domains().get_by_id(domain.id).status is DomainStatus.ACTIVE
    assert len(world.cloudflare.records) == 1


def test_a_failed_issuance_leaves_the_site_on_http_and_records_the_error(
    world: IngressWorld,
) -> None:
    domain = world.create()
    world.issuer.fail_with = AcmeError("ACME error: rate limited")
    plan = world.ingress().preview(domain.id)
    with pytest.raises(UpstreamError, match="rate limited"):
        world.ingress().apply(domain.id, plan.plan_hash, ACTOR)
    assert "SSLEngine" not in world.helper.vhosts[HOST]
    assert world.domains().get_by_id(domain.id).status is DomainStatus.ERROR
    assert "certificate.issue_failed" in actions(world)


def test_disabling_tls_removes_the_certificate_and_the_tls_vhost(world: IngressWorld) -> None:
    domain = world.create()
    world.apply(domain)
    world.domains().update("demo", DEV, domain.id, DomainPatch(tls_enabled=False), ACTOR)
    plan = world.ingress().preview(domain.id)
    assert plan.tls.action == "remove"
    world.ingress().apply(domain.id, plan.plan_hash, ACTOR)
    assert "SSLEngine" not in world.helper.vhosts[HOST]
    assert world.helper.certificates == {}
    assert world.certificates().status_for(domain.id) is None


def test_a_certificate_missing_on_the_host_is_issued_again(world: IngressWorld) -> None:
    domain = world.create()
    world.apply(domain)
    world.helper.certificates.clear()  # e.g. the host was rebuilt
    plan = world.ingress().preview(domain.id)
    assert plan.tls.action == "issue"
    world.ingress().apply(domain.id, plan.plan_hash, ACTOR)
    assert world.issuer.issued == [HOST, HOST]


def test_remove_tears_down_vhost_certificate_and_only_owned_dns(world: IngressWorld) -> None:
    domain = world.create()
    world.apply(domain)
    foreign = world.cloudflare.add_record(
        type="AAAA", name=HOST, content="2001:db8::1", comment=None
    )
    service = world.ingress()
    plan = service.preview(domain.id, "remove")
    assert plan.operation == "remove"
    assert (plan.dns.action, plan.vhost.action, plan.tls.action) == (
        PlanAction.DELETE,
        PlanAction.DELETE,
        "remove",
    )
    assert "-<VirtualHost *:443>" in plan.vhost.diff
    service.remove(domain.id, plan.plan_hash, ACTOR)
    assert world.helper.vhosts == {} and world.helper.certificates == {}
    assert world.cloudflare.records == [foreign]
    stored = world.domains().get_by_id(domain.id)
    assert stored.status is DomainStatus.DRAFT and stored.cloudflare_record_id is None
    assert world.certificates().status_for(domain.id) is None
    assert "domain.remove" in actions(world)
    # Now the definition can be deleted.
    world.domains().delete("demo", DEV, domain.id, ACTOR)


def test_remove_needs_its_own_reviewed_plan(world: IngressWorld) -> None:
    domain = world.create()
    world.apply(domain)
    apply_plan = world.ingress().preview(domain.id, "apply")
    with pytest.raises(ConflictError):
        world.ingress().remove(domain.id, apply_plan.plan_hash, ACTOR)
    assert HOST in world.helper.vhosts


def test_cname_and_proxied_records_are_planned_as_requested(world: IngressWorld) -> None:
    domain = world.create(
        dns_record_type=DnsRecordType.CNAME, dns_target="edge.example.net", dns_proxied=True
    )
    plan = world.ingress().preview(domain.id)
    assert plan.dns.desired is not None
    assert (plan.dns.desired.record_type, plan.dns.desired.content, plan.dns.desired.proxied) == (
        "CNAME",
        "edge.example.net",
        True,
    )


def test_a_missing_server_address_is_a_configuration_error(world: IngressWorld) -> None:
    world.settings = world.settings.model_copy(update={"public_ipv4": None})
    domain = world.create()
    with pytest.raises(ConfigurationError, match="PUBLIC_IPV4"):
        world.ingress().preview(domain.id)


def test_a_rejected_cloudflare_token_is_reported_without_the_token(world: IngressWorld) -> None:
    domain = world.create()
    world.cloudflare.reject_auth = True
    with pytest.raises(UpstreamError) as raised:
        world.ingress().preview(domain.id)
    assert "token" in str(raised.value)
    assert world.cloudflare.token not in str(raised.value)


def test_manual_certificate_issue_needs_an_applied_domain_with_tls(world: IngressWorld) -> None:
    domain = world.create()
    with pytest.raises(ConflictError, match="apply the domain"):
        world.ingress().issue_certificate(domain.id, ACTOR)
    world.apply(domain)
    status = world.ingress().issue_certificate(domain.id, ACTOR)
    assert status.days_remaining >= 89
    assert world.issuer.issued == [HOST, HOST]
    plain = world.create("plain.example.com", tls_enabled=False)
    world.apply(plain)
    with pytest.raises(ConflictError, match="TLS is disabled"):
        world.ingress().issue_certificate(plain.id, ACTOR)
