"""A complete in-memory "world" for service-level ingress tests."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from backend.config_ingress import IngressSettings
from backend.domain.actor import Actor
from backend.domain.enums import ActorType, EnvironmentName
from backend.domain.ingress import IngressPlanView
from backend.repositories.database import UnitOfWork
from backend.repositories.models import Domain
from backend.services.certificates import CertificateService, RenewalPolicy
from backend.services.domains import DomainDraft, DomainService
from backend.services.ingress import IngressPorts, IngressService
from tests.ingress_support import HOST, FakeCloudflare, FakeHelper, FakeIssuer

ACTOR = Actor(ActorType.USER, "1")
DEFAULT_IPV4 = "203.0.113.7"


class IngressWorld:
    """Real services on the test database with fake Cloudflare, helper and CA."""

    def __init__(
        self, uow_factory: Callable[[], UnitOfWork], clock: Callable[[], datetime]
    ) -> None:
        self.clock = clock
        self.uow_factory = uow_factory
        self.helper = FakeHelper()
        self.cloudflare = FakeCloudflare()
        self.issuer = FakeIssuer(clock)
        self.settings = IngressSettings(public_ipv4=DEFAULT_IPV4)
        self.policy = RenewalPolicy()

    def domains(self) -> DomainService:
        """A domain service."""
        return DomainService(self.uow_factory(), self.clock)

    def certificates(self) -> CertificateService:
        """A certificate service issuing through ``FakeIssuer``."""
        return CertificateService(
            self.uow_factory(), self.helper, lambda _domain: self.issuer, self.policy, self.clock
        )

    def ingress(self) -> IngressService:
        """The orchestrator."""
        ports = IngressPorts(self.helper, lambda _domain: self.cloudflare.dns())
        return IngressService(self.domains(), self.certificates(), ports, self.settings, self.clock)

    def create(self, hostname: str = HOST, **fields: Any) -> Domain:
        """Define a domain in the demo project's development environment."""
        draft = DomainDraft(
            hostname=hostname,
            service=fields.pop("service", "web"),
            container_port=fields.pop("container_port", 8080),
            published_port=fields.pop("published_port", 20001),
            **fields,
        )
        return self.domains().create("demo", EnvironmentName.DEVELOPMENT, draft, ACTOR)

    def apply(self, domain: Domain) -> IngressPlanView:
        """Preview then apply, as the UI does."""
        service = self.ingress()
        plan = service.preview(domain.id)
        return service.apply(domain.id, plan.plan_hash, ACTOR)
