"""Queries for domains, their certificates and their latest status checks."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from shiftcodes.repositories.models import Domain, DomainCertificate, DomainCheck


class DomainRepository:
    """Queries for ``Domain``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, domain: Domain) -> Domain:
        """Persist ``domain`` and assign its id."""
        self._session.add(domain)
        self._session.flush()
        return domain

    def get(self, domain_id: int) -> Domain | None:
        """Return the domain with ``domain_id`` or ``None``."""
        return self._session.get(Domain, domain_id)

    def get_by_hostname(self, hostname: str) -> Domain | None:
        """Return the domain with exactly this canonical hostname."""
        return self._session.scalar(select(Domain).where(Domain.hostname == hostname))

    def list_for_environment(self, environment_id: int) -> Sequence[Domain]:
        """Return the domains of an environment ordered by hostname."""
        query = select(Domain).where(Domain.environment_id == environment_id)
        return self._session.scalars(query.order_by(Domain.hostname)).all()

    def list_all(self) -> Sequence[Domain]:
        """Return every domain ordered by hostname."""
        return self._session.scalars(select(Domain).order_by(Domain.hostname)).all()

    def delete(self, domain: Domain) -> None:
        """Delete one domain (certificate and check rows cascade)."""
        self._session.delete(domain)
        self._session.flush()


class CertificateRepository:
    """Queries for ``DomainCertificate`` (at most one per domain)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_for_domain(self, domain_id: int) -> DomainCertificate | None:
        """Return the certificate row of a domain or ``None``."""
        query = select(DomainCertificate).where(DomainCertificate.domain_id == domain_id)
        return self._session.scalar(query)

    def add(self, certificate: DomainCertificate) -> DomainCertificate:
        """Persist ``certificate`` and assign its id."""
        self._session.add(certificate)
        self._session.flush()
        return certificate

    def list_expiring_before(self, moment: datetime) -> Sequence[DomainCertificate]:
        """Return certificates whose ``not_after`` is at or before ``moment``, soonest first."""
        query = select(DomainCertificate).where(DomainCertificate.not_after <= moment)
        return self._session.scalars(query.order_by(DomainCertificate.not_after)).all()

    def list_all(self) -> Sequence[DomainCertificate]:
        """Return every certificate, soonest expiry first."""
        query = select(DomainCertificate).order_by(DomainCertificate.not_after)
        return self._session.scalars(query).all()

    def delete(self, certificate: DomainCertificate) -> None:
        """Delete one certificate row."""
        self._session.delete(certificate)
        self._session.flush()


class DomainCheckRepository:
    """Queries for ``DomainCheck`` (the latest result per domain)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_for_domain(self, domain_id: int) -> DomainCheck | None:
        """Return the latest check of a domain or ``None``."""
        return self._session.scalar(select(DomainCheck).where(DomainCheck.domain_id == domain_id))

    def add(self, check: DomainCheck) -> DomainCheck:
        """Persist ``check`` and assign its id."""
        self._session.add(check)
        self._session.flush()
        return check
