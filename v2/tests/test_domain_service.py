import pytest
from sqlalchemy import Engine

from shiftcodes.domain.errors import ConflictError, NotFoundError, ValidationError
from shiftcodes.domain.ingress import DnsRecordType, DomainStatus
from shiftcodes.repositories.database import UnitOfWork, create_session_factory
from shiftcodes.services.audit import AuditService
from shiftcodes.services.domains import DomainDraft, DomainPatch, DomainService
from shiftcodes.services.environments import EnvironmentService, EnvironmentUpdate
from shiftcodes.services.projects import ProjectService
from tests.api_support import FakeClock
from tests.conftest import DEV, PROD
from tests.ingress_world import ACTOR


@pytest.fixture
def service(engine: Engine, clock: FakeClock, project: str) -> DomainService:
    return DomainService(UnitOfWork(create_session_factory(engine)), clock)


def draft(hostname: str = "app.example.com", **fields: object) -> DomainDraft:
    values: dict[str, object] = {
        "hostname": hostname,
        "service": "web",
        "container_port": 8080,
        "published_port": 20001,
    }
    values.update(fields)
    return DomainDraft(**values)  # type: ignore[arg-type]


def test_create_normalizes_the_hostname_and_starts_as_draft(service: DomainService) -> None:
    domain = service.create("demo", DEV, draft("Bücher.Example.com."), ACTOR)
    assert domain.hostname == "xn--bcher-kva.example.com"
    assert domain.status is DomainStatus.DRAFT
    assert domain.dns_record_type is DnsRecordType.A and domain.tls_enabled
    assert [d.id for d in service.list("demo", DEV)] == [domain.id]
    assert service.list("demo", PROD) == []


def test_hostnames_are_unique_across_projects_and_environments(
    service: DomainService, uow: UnitOfWork
) -> None:
    service.create("demo", DEV, draft(), ACTOR)
    ProjectService(uow).create("other", "Other", "octo/other")
    for slug, env in (("demo", DEV), ("demo", PROD), ("other", DEV)):
        with pytest.raises(ConflictError, match="already in use"):
            service.create(slug, env, draft("APP.example.com"), ACTOR)  # case-insensitive


def test_invalid_input_is_rejected(service: DomainService) -> None:
    for bad in (
        draft("*.example.com"),  # wildcards are off by default
        draft("not a host"),
        draft("1.2.3.4"),
        draft(service="bad name"),
        draft(service="x" * 64),
        draft(container_port=0),
        draft(container_port=70000),
        draft(published_port=80),  # below 1024
        draft(dns_record_type=DnsRecordType.A, dns_target="2001:db8::1"),
        draft(dns_record_type=DnsRecordType.CNAME),
    ):
        with pytest.raises(ValidationError):
            service.create("demo", DEV, bad, ACTOR)
    assert service.list("demo", DEV) == []


def test_wildcards_need_an_explicit_switch(engine: Engine, clock: FakeClock, project: str) -> None:
    allowed = DomainService(UnitOfWork(create_session_factory(engine)), clock, allow_wildcards=True)
    assert allowed.create("demo", DEV, draft("*.example.com"), ACTOR).hostname == "*.example.com"


def test_the_service_must_belong_to_the_environment_when_it_lists_services(
    service: DomainService, uow: UnitOfWork
) -> None:
    EnvironmentService(uow).update("demo", DEV, EnvironmentUpdate(services=["web", "worker"]))
    service.create("demo", DEV, draft(service="worker"), ACTOR)
    with pytest.raises(ValidationError, match="not part of the environment"):
        service.create("demo", DEV, draft("b.example.com", service="db"), ACTOR)


def test_unknown_project_or_environment_is_not_found(service: DomainService) -> None:
    with pytest.raises(NotFoundError):
        service.create("missing", DEV, draft(), ACTOR)
    with pytest.raises(NotFoundError):
        service.list("missing", DEV)


def test_update_changes_only_the_given_fields_and_can_reset_the_target(
    service: DomainService,
) -> None:
    domain = service.create("demo", DEV, draft(dns_target="198.51.100.5"), ACTOR)
    updated = service.update(
        "demo", DEV, domain.id, DomainPatch(published_port=20005, dns_proxied=True), ACTOR
    )
    assert (updated.published_port, updated.dns_proxied, updated.dns_target) == (
        20005,
        True,
        "198.51.100.5",
    )
    reset = service.update("demo", DEV, domain.id, DomainPatch(clear_dns_target=True), ACTOR)
    assert reset.dns_target is None
    with pytest.raises(ValidationError):
        service.update(
            "demo", DEV, domain.id, DomainPatch(dns_record_type=DnsRecordType.CNAME), ACTOR
        )
    assert service.get("demo", DEV, domain.id).dns_record_type is DnsRecordType.A


def test_domains_of_another_environment_are_not_reachable_by_id(service: DomainService) -> None:
    domain = service.create("demo", DEV, draft(), ACTOR)
    with pytest.raises(NotFoundError):
        service.get("demo", PROD, domain.id)
    with pytest.raises(NotFoundError):
        service.update("demo", PROD, domain.id, DomainPatch(dns_proxied=True), ACTOR)
    with pytest.raises(NotFoundError):
        service.delete("demo", PROD, domain.id, ACTOR)


def test_only_a_draft_domain_can_be_deleted(service: DomainService) -> None:
    domain = service.create("demo", DEV, draft(), ACTOR)
    service.record_applied(domain.id, zone_id="z", record_id="r")
    with pytest.raises(ConflictError, match="remove the domain"):
        service.delete("demo", DEV, domain.id, ACTOR)
    service.record_removed(domain.id)
    service.delete("demo", DEV, domain.id, ACTOR)
    assert service.list("demo", DEV) == []


def test_state_transitions_and_audit(service: DomainService, uow: UnitOfWork) -> None:
    domain = service.create("demo", DEV, draft(), ACTOR)
    service.update("demo", DEV, domain.id, DomainPatch(published_port=20002), ACTOR)
    applied = service.record_applied(domain.id, zone_id="zone1", record_id="rec1")
    assert applied.status is DomainStatus.ACTIVE and applied.cloudflare_record_id == "rec1"
    failed = service.record_error(domain.id, "x" * 900)
    assert failed.status is DomainStatus.ERROR and len(failed.status_message or "") == 500
    events = AuditService(uow).list(limit=20)
    assert [e.action for e in events] == ["domain.update", "domain.create"]
    update = events[0]
    assert update.details == {"hostname": "app.example.com", "fields": ["published_port"]}
    assert update.target_type == "domain" and update.target_id == str(domain.id)
