"""Certificate use cases: issue, renew 30 days before expiry, and monitor expiry."""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from cryptography import x509
from cryptography.hazmat.primitives import hashes

from shiftcodes.adapters.acme.issuer import IssuedCertificate
from shiftcodes.adapters.errors import AdapterError
from shiftcodes.adapters.host_helper_client import HostHelper
from shiftcodes.domain.actor import Actor
from shiftcodes.domain.audit import AuditEntry, AuditTarget
from shiftcodes.domain.clock import Clock, utcnow
from shiftcodes.domain.errors import DomainError, UpstreamError
from shiftcodes.domain.ingress import CertificateState, DomainStatus
from shiftcodes.repositories.database import UnitOfWork
from shiftcodes.repositories.models import Domain, DomainCertificate
from shiftcodes.services.audit import add_audit_event

logger = logging.getLogger(__name__)
RENEWAL_ACTOR = "ingress-renewal"
WARNING_DAYS = 14
CRITICAL_DAYS = 3
MAX_ERROR_LENGTH = 500


class CertificateIssuer(Protocol):
    """Obtains a certificate and key for a hostname (ACME in production, fakes in tests)."""

    @property
    def challenge_type(self) -> str:
        """``dns-01`` or ``http-01``."""

    def issue(self, hostname: str) -> IssuedCertificate:
        """Issue a certificate for ``hostname``."""


IssuerFactory = Callable[[Domain], CertificateIssuer]


@dataclass(frozen=True, slots=True)
class CertificateStatus:
    """Expiry health of one domain's certificate."""

    domain_id: int
    hostname: str
    not_after: datetime
    days_remaining: int
    state: CertificateState
    last_attempt_at: datetime | None
    last_error: str | None


@dataclass(frozen=True, slots=True)
class RenewalOutcome:
    """Result of one renewal attempt."""

    domain_id: int
    hostname: str
    ok: bool
    message: str


def certificate_state(
    not_after: datetime, now: datetime, renew_before: timedelta
) -> CertificateState:
    """Classify the expiry of a certificate at ``now``."""
    remaining = not_after - now
    if remaining <= timedelta(0):
        return CertificateState.EXPIRED
    if remaining < timedelta(days=CRITICAL_DAYS):
        return CertificateState.CRITICAL
    if remaining < timedelta(days=WARNING_DAYS):
        return CertificateState.WARNING
    if remaining <= renew_before:
        return CertificateState.RENEWAL_DUE
    return CertificateState.OK


@dataclass(frozen=True, slots=True)
class RenewalPolicy:
    """When certificates are renewed and how soon a failed renewal is retried."""

    renew_before: timedelta = timedelta(days=30)
    retry_after: timedelta = timedelta(hours=6)


class CertificateService:
    """Issues certificates through an issuer, installs them via the helper and tracks expiry."""

    def __init__(
        self,
        uow: UnitOfWork,
        helper: HostHelper,
        issuers: IssuerFactory,
        policy: RenewalPolicy = RenewalPolicy(),  # noqa: B008 - frozen value object
        clock: Clock = utcnow,
    ) -> None:
        self._uow = uow
        self._helper = helper
        self._issuers = issuers
        self._clock = clock
        self._renew_before = policy.renew_before
        self._retry_after = policy.retry_after

    def issue(self, domain: Domain, actor: Actor) -> CertificateStatus:
        """Obtain a new certificate for ``domain`` and install it on the host."""
        issuer = self._issuers(domain)
        try:
            issued = issuer.issue(domain.hostname)
            self._helper.install_certificate(
                domain.hostname, issued.fullchain_pem, issued.private_key_pem
            )
        except AdapterError as error:
            self._record_failure(domain, actor, str(error))
            raise UpstreamError(f"certificate issuance failed: {error}") from error
        return self._record_success(domain, actor, issued, issuer.challenge_type)

    def renew_due(self) -> list[RenewalOutcome]:
        """Renew every certificate that expires within the renewal window."""
        now = self._clock()
        outcomes: list[RenewalOutcome] = []
        with self._uow:
            candidates = list(self._uow.certificates.list_expiring_before(now + self._renew_before))
        for certificate in candidates:
            if self._recently_failed(certificate, now):
                continue
            outcomes.append(self._renew_one(certificate.domain_id))
        return outcomes

    def report(self) -> Sequence[CertificateStatus]:
        """Expiry health of all certificates, soonest expiry first."""
        now = self._clock()
        with self._uow:
            rows = list(self._uow.certificates.list_all())
            result = []
            for row in rows:
                domain = self._uow.domains.get(row.domain_id)
                if domain is not None:
                    result.append(self._status(domain, row, now))
            return result

    def status_for(self, domain_id: int) -> CertificateStatus | None:
        """Expiry health of one domain's certificate, or ``None`` if it has none."""
        with self._uow:
            row = self._uow.certificates.get_for_domain(domain_id)
            domain = self._uow.domains.get(domain_id)
            if row is None or domain is None:
                return None
            return self._status(domain, row, self._clock())

    def has_valid_certificate(self, domain_id: int, *, margin: timedelta) -> bool:
        """Whether a certificate exists that is still valid for at least ``margin``."""
        status = self.status_for(domain_id)
        return status is not None and status.not_after - self._clock() > margin

    def discard(self, domain: Domain) -> bool:
        """Remove the certificate from the host and forget it; returns whether one existed."""
        removed = self._helper.remove_certificate(domain.hostname)
        with self._uow:
            row = self._uow.certificates.get_for_domain(domain.id)
            if row is not None:
                self._uow.certificates.delete(row)
        return removed or row is not None

    # --- internals ---------------------------------------------------------------------------

    def _renew_one(self, domain_id: int) -> RenewalOutcome:
        actor = Actor.system(RENEWAL_ACTOR)
        with self._uow:
            domain = self._uow.domains.get(domain_id)
        if domain is None or not domain.tls_enabled or domain.status is not DomainStatus.ACTIVE:
            return RenewalOutcome(domain_id, domain.hostname if domain else "", False, "skipped")
        try:
            status = self.issue(domain, actor)
        except DomainError as error:
            logger.warning("certificate renewal failed", extra={"domain_id": domain_id})
            return RenewalOutcome(domain_id, domain.hostname, False, str(error))
        return RenewalOutcome(domain_id, domain.hostname, True, status.not_after.isoformat())

    def _recently_failed(self, certificate: DomainCertificate, now: datetime) -> bool:
        attempt = certificate.last_attempt_at
        return (
            certificate.last_error is not None
            and attempt is not None
            and now - attempt < self._retry_after
        )

    def _record_success(
        self, domain: Domain, actor: Actor, issued: IssuedCertificate, challenge: str
    ) -> CertificateStatus:
        leaf = x509.load_pem_x509_certificate(issued.fullchain_pem.encode())
        now = self._clock()
        with self._uow:
            row = self._uow.certificates.get_for_domain(domain.id)
            if row is None:
                row = self._uow.certificates.add(_new_row(domain.id, leaf, challenge, now))
            else:
                _fill_row(row, leaf, challenge, now)
            action = "certificate.issue"
            details: dict[str, object] = {
                "hostname": domain.hostname,
                "not_after": row.not_after.isoformat(),
                "challenge": challenge,
            }
            self._audit(actor, action, domain, details)
            return self._status(domain, row, now)

    def _record_failure(self, domain: Domain, actor: Actor, message: str) -> None:
        now = self._clock()
        with self._uow:
            row = self._uow.certificates.get_for_domain(domain.id)
            if row is not None:
                row.last_attempt_at = now
                row.attempts += 1
                row.last_error = message[:MAX_ERROR_LENGTH]
            details: dict[str, object] = {"hostname": domain.hostname, "error": message[:200]}
            self._audit(actor, "certificate.issue_failed", domain, details)

    def _status(self, domain: Domain, row: DomainCertificate, now: datetime) -> CertificateStatus:
        return build_status(domain, row, now, self._renew_before)

    def _audit(self, actor: Actor, action: str, domain: Domain, details: dict[str, object]) -> None:
        target = AuditTarget("domain", str(domain.id))
        add_audit_event(self._uow, AuditEntry(actor, action, target, details), self._clock)


def build_status(
    domain: Domain, row: DomainCertificate, now: datetime, renew_before: timedelta
) -> CertificateStatus:
    """Expiry health of ``row`` at ``now``."""
    remaining = row.not_after - now
    return CertificateStatus(
        domain_id=domain.id,
        hostname=domain.hostname,
        not_after=row.not_after,
        days_remaining=int(remaining.total_seconds() // 86400),
        state=certificate_state(row.not_after, now, renew_before),
        last_attempt_at=row.last_attempt_at,
        last_error=row.last_error,
    )


def _new_row(
    domain_id: int, leaf: x509.Certificate, challenge: str, now: datetime
) -> DomainCertificate:
    row = DomainCertificate(domain_id=domain_id)
    _fill_row(row, leaf, challenge, now)
    return row


def _fill_row(
    row: DomainCertificate, leaf: x509.Certificate, challenge: str, now: datetime
) -> None:
    row.serial = f"{leaf.serial_number:x}"
    row.fingerprint_sha256 = leaf.fingerprint(hashes.SHA256()).hex()
    row.challenge = challenge
    row.not_before = leaf.not_valid_before_utc
    row.not_after = leaf.not_valid_after_utc
    row.issued_at = now
    row.last_attempt_at = now
    row.attempts = 0
    row.last_error = None
