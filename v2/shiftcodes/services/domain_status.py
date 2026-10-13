"""Read-only status of domains for the API: stored checks and certificate expiry."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from shiftcodes.domain.clock import Clock, utcnow
from shiftcodes.domain.enums import EnvironmentName
from shiftcodes.repositories.database import UnitOfWork
from shiftcodes.repositories.models import Domain, DomainCertificate
from shiftcodes.services.certificates import CertificateStatus, RenewalPolicy, build_status
from shiftcodes.services.domain_checks import CheckResult
from shiftcodes.services.domains import DomainService


@dataclass(frozen=True, slots=True)
class DomainStatus:
    """A domain with its certificate health and its latest check."""

    domain: Domain
    certificate: CertificateStatus | None
    check: CheckResult | None


class DomainStatusService:
    """Combines stored rows into status views; it never contacts a provider."""

    def __init__(
        self,
        uow: UnitOfWork,
        domains: DomainService,
        policy: RenewalPolicy = RenewalPolicy(),  # noqa: B008 - frozen value object
        clock: Clock = utcnow,
    ) -> None:
        self._uow = uow
        self._domains = domains
        self._policy = policy
        self._clock = clock

    def status(self, slug: str, env: EnvironmentName, domain_id: int) -> DomainStatus:
        """Status of one domain of an environment."""
        domain = self._domains.get(slug, env, domain_id)
        return self._build(domain)

    def list(self, slug: str, env: EnvironmentName) -> Sequence[DomainStatus]:
        """Status of every domain of an environment."""
        return [self._build(domain) for domain in self._domains.list(slug, env)]

    def certificates(self) -> Sequence[CertificateStatus]:
        """Expiry monitoring: all certificates, soonest expiry first."""
        now = self._clock()
        with self._uow:
            result = []
            for row in self._uow.certificates.list_all():
                domain = self._uow.domains.get(row.domain_id)
                if domain is not None:
                    result.append(self._certificate_status(domain, row, now))
            return result

    def _build(self, domain: Domain) -> DomainStatus:
        now = self._clock()
        with self._uow:
            row = self._uow.certificates.get_for_domain(domain.id)
            check = self._uow.domain_checks.get_for_domain(domain.id)
            certificate = self._certificate_status(domain, row, now) if row else None
            latest = (
                CheckResult(domain.id, check.checked_at, check.ok, dict(check.results))
                if check
                else None
            )
        return DomainStatus(domain, certificate, latest)

    def _certificate_status(
        self, domain: Domain, row: DomainCertificate, now: datetime
    ) -> CertificateStatus:
        return build_status(domain, row, now, self._policy.renew_before)
