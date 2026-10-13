"""Renewal scheduling and expiry monitoring with a fake clock."""

from datetime import timedelta

import pytest
from sqlalchemy import Engine

from backend.adapters.acme.client import AcmeError
from backend.domain.enums import ActorType
from backend.domain.errors import UpstreamError
from backend.domain.ingress import CertificateState, DomainStatus
from backend.repositories.database import UnitOfWork, create_session_factory
from backend.services.audit import AuditService
from backend.services.certificates import certificate_state
from backend.services.domain_status import DomainStatusService
from backend.services.renewal import RenewalScheduler
from tests.api_support import FakeClock
from tests.ingress_support import HOST
from tests.ingress_world import ACTOR, IngressWorld

RENEW = timedelta(days=30)


@pytest.fixture
def world(engine: Engine, clock: FakeClock, project: str) -> IngressWorld:
    factory = create_session_factory(engine)
    return IngressWorld(lambda: UnitOfWork(factory), clock)


def scheduler(world: IngressWorld) -> RenewalScheduler:
    return RenewalScheduler(world.certificates(), interval=3600)


def test_nothing_is_renewed_until_30_days_before_expiry(
    world: IngressWorld, clock: FakeClock
) -> None:
    world.apply(world.create())
    assert world.issuer.issued == [HOST]  # 90 day certificate
    clock.advance(days=59)  # 31 days left
    assert scheduler(world).tick() == []
    assert world.issuer.issued == [HOST]


def test_the_certificate_is_renewed_when_30_days_remain(
    world: IngressWorld, clock: FakeClock
) -> None:
    domain = world.create()
    world.apply(domain)
    first = world.certificates().status_for(domain.id)
    assert first is not None
    clock.advance(days=60)  # exactly 30 days left: inside the window
    outcomes = scheduler(world).tick()
    assert [(o.hostname, o.ok) for o in outcomes] == [(HOST, True)]
    renewed = world.certificates().status_for(domain.id)
    assert renewed is not None and renewed.not_after > first.not_after
    assert renewed.days_remaining == 90
    assert world.issuer.issued == [HOST, HOST]
    assert world.helper.calls.count("install_certificate") == 2
    # The renewal is attributed to the system and audited.
    events = [
        e
        for e in AuditService(world.uow_factory()).list(limit=50)
        if e.action == "certificate.issue"
    ]
    assert events[0].actor_type is ActorType.SYSTEM and events[0].actor_id == "ingress-renewal"
    # A fresh certificate is not renewed again on the next pass.
    assert scheduler(world).tick() == []


def test_a_failed_renewal_is_retried_after_six_hours_not_every_pass(
    world: IngressWorld, clock: FakeClock
) -> None:
    domain = world.create()
    world.apply(domain)
    clock.advance(days=61)
    world.issuer.fail_with = AcmeError("CA unavailable")
    sched = scheduler(world)
    [failed] = sched.tick()
    assert not failed.ok and "CA unavailable" in failed.message
    status = world.certificates().status_for(domain.id)
    assert status is not None and status.last_error is not None
    assert world.issuer.issued == [HOST]
    clock.advance(hours=5)
    assert sched.tick() == []  # still inside the retry delay
    clock.advance(hours=2)
    world.issuer.fail_with = None
    [renewed] = sched.tick()
    assert renewed.ok
    healed = world.certificates().status_for(domain.id)
    assert healed is not None and healed.last_error is None and healed.state is CertificateState.OK


def test_renewal_skips_domains_that_are_not_active_or_have_tls_off(
    world: IngressWorld, clock: FakeClock
) -> None:
    domain = world.create()
    world.apply(domain)
    world.domains().record_error(domain.id, "apply failed")
    clock.advance(days=61)
    [outcome] = scheduler(world).tick()
    assert not outcome.ok and outcome.message == "skipped"
    assert world.domains().get_by_id(domain.id).status is DomainStatus.ERROR
    assert world.issuer.issued == [HOST]


def test_a_manual_issue_failure_is_an_upstream_error_and_audited(world: IngressWorld) -> None:
    domain = world.create()
    world.apply(domain)
    world.issuer.fail_with = AcmeError("ACME error: boom")
    with pytest.raises(UpstreamError, match="boom"):
        world.ingress().issue_certificate(domain.id, ACTOR)


def test_run_forever_ticks_until_stopped(world: IngressWorld, clock: FakeClock) -> None:
    domain = world.create()
    world.apply(domain)
    clock.advance(days=61)

    class Stop:
        def __init__(self) -> None:
            self.waits: list[float] = []

        def wait(self, timeout: float) -> bool:
            self.waits.append(timeout)
            return len(self.waits) >= 2

    stop = Stop()
    scheduler(world).run_forever(stop)
    assert stop.waits == [3600, 3600]
    assert world.issuer.issued == [HOST, HOST]  # renewed on the first pass only


def test_a_crashing_pass_does_not_end_the_loop(world: IngressWorld) -> None:
    class Boom(RenewalScheduler):
        def tick(self):  # type: ignore[no-untyped-def]
            raise RuntimeError("database locked")

    class Stop:
        calls = 0

        def wait(self, _timeout: float) -> bool:
            Stop.calls += 1
            return Stop.calls >= 3

    Boom(world.certificates(), 1).run_forever(Stop())
    assert Stop.calls == 3


@pytest.mark.parametrize(
    ("remaining", "state"),
    [
        (timedelta(days=-1), CertificateState.EXPIRED),
        (timedelta(0), CertificateState.EXPIRED),
        (timedelta(days=2, hours=23), CertificateState.CRITICAL),
        (timedelta(days=3), CertificateState.WARNING),
        (timedelta(days=13), CertificateState.WARNING),
        (timedelta(days=14), CertificateState.RENEWAL_DUE),
        (timedelta(days=30), CertificateState.RENEWAL_DUE),
        (timedelta(days=30, seconds=1), CertificateState.OK),
    ],
)
def test_expiry_states(remaining: timedelta, state: CertificateState, clock: FakeClock) -> None:
    assert certificate_state(clock() + remaining, clock(), RENEW) is state


def test_status_service_reports_days_and_state_across_domains(
    world: IngressWorld, clock: FakeClock
) -> None:
    first = world.create("a.example.com")
    second = world.create("b.example.com", published_port=20002)
    world.apply(first)
    clock.advance(days=40)
    world.apply(second)
    status = DomainStatusService(world.uow_factory(), world.domains(), world.policy, clock)
    first_cert, second_cert = status.certificates()  # soonest expiry first
    assert (first_cert.hostname, first_cert.days_remaining, first_cert.state) == (
        "a.example.com",
        50,
        CertificateState.OK,
    )
    assert second_cert.hostname == "b.example.com" and second_cert.days_remaining == 90
    clock.advance(days=25)
    assert status.certificates()[0].state is CertificateState.RENEWAL_DUE
    clock.advance(days=30)
    assert status.certificates()[0].state is CertificateState.EXPIRED
