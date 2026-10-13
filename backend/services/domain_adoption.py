"""Adopting domains: record a host name that already works as linked to a project environment.

Adoption only writes the platform's own database. It never touches DNS, Apache or TLS: the
Cloudflare record and the Apache site were made by hand and stay that way (the domain is marked
``adopted``, in contrast to ``managed`` domains Jendock created and may change). The facts come
from the last discovery, not from the caller, so what is recorded is what was really found.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from backend.domain.actor import Actor
from backend.domain.audit import AuditEntry, AuditTarget
from backend.domain.clock import Clock, utcnow
from backend.domain.enums import EnvironmentName
from backend.domain.errors import ConflictError, DomainError, NotFoundError, ValidationError
from backend.domain.hostnames import Hostname
from backend.domain.ingress import (
    DnsRecordType,
    DomainOrigin,
    DomainStatus,
    validate_dns_target,
    validate_port,
)
from backend.domain.values import validate_service_names
from backend.repositories.database import UnitOfWork
from backend.repositories.models import DiscoveredDomain, Domain
from backend.services.audit import add_audit_event
from backend.services.domains import check_service
from backend.services.lookup import require_environment

MAX_SERVICE = 63


@dataclass(frozen=True, slots=True)
class AdoptRequest:
    """Where a discovered host name belongs; the service and ports say which container answers."""

    hostname: str
    service: str
    container_port: int
    published_port: int


@dataclass(frozen=True, slots=True)
class Skipped:
    """A host name the bulk adoption left alone, and why."""

    hostname: str
    reason: str


@dataclass(frozen=True, slots=True)
class BulkResult:
    """Outcome of "adopt all suggested"."""

    adopted: tuple[Domain, ...]
    skipped: tuple[Skipped, ...]


class AdoptionService:
    """Links discovered host names to projects without changing anything outside the database."""

    def __init__(self, uow: UnitOfWork, clock: Clock = utcnow) -> None:
        self._uow = uow
        self._clock = clock

    def adopt(self, slug: str, env: EnvironmentName, request: AdoptRequest, actor: Actor) -> Domain:
        """Record ``request.hostname`` as an adopted domain of ``slug``/``env``."""
        hostname = Hostname.parse(request.hostname, allow_wildcard=True).value
        service = validate_service_names([request.service])[0]
        if len(service) > MAX_SERVICE:
            raise ValidationError("service name is too long")
        container_port = validate_port(request.container_port, "container_port")
        published_port = validate_port(request.published_port, "published_port")
        with self._uow:
            environment = require_environment(self._uow, slug, env)
            check_service(environment, service)
            found = self._uow.discovery.get_by_hostname(hostname)
            if found is None:
                raise NotFoundError(
                    f"{hostname} was not found by the last discovery; refresh from Cloudflare first"
                )
            if self._uow.domains.get_by_hostname(hostname) is not None:
                raise ConflictError(f"hostname is already in use: {hostname}")
            now = self._clock()
            checked = AdoptRequest(hostname, service, container_port, published_port)
            domain = self._uow.domains.add(_adopted(environment.id, found, checked, now))
            details: dict[str, object] = {
                "hostname": hostname,
                "project": slug,
                "environment": env.value,
                "origin": DomainOrigin.ADOPTED.value,
            }
            entry = AuditEntry(
                actor, "domain.adopt", AuditTarget("domain", str(domain.id)), details
            )
            add_audit_event(self._uow, entry, self._clock)
            return domain

    def adopt_suggested(self, hostnames: Sequence[str] | None, actor: Actor) -> BulkResult:
        """Adopt every unlinked host name whose suggestion is certain (or only ``hostnames``).

        "Certain" means the Apache upstream port is the published port of exactly one container
        (confidence ``high``); weaker suggestions are never adopted in bulk.
        """
        with self._uow:
            rows = list(self._uow.discovery.list_obvious_unlinked())
        wanted = None if hostnames is None else {name.strip().lower() for name in hostnames}
        adopted: list[Domain] = []
        skipped: list[Skipped] = []
        for row in rows:
            if wanted is not None and row.hostname not in wanted:
                continue
            try:
                adopted.append(self._adopt_row(row, actor))
            except DomainError as error:
                skipped.append(Skipped(row.hostname, str(error)))
        if wanted is not None:
            seen = {row.hostname for row in rows}
            skipped.extend(
                Skipped(name, "not an unlinked host name with a certain suggestion")
                for name in sorted(wanted - seen)
            )
        return BulkResult(tuple(adopted), tuple(skipped))

    def _adopt_row(self, row: DiscoveredDomain, actor: Actor) -> Domain:
        project, environment = row.suggested_project, row.suggested_environment
        service = row.suggested_service
        container_port, published_port = row.suggested_container_port, row.suggested_published_port
        if not (project and environment and service and container_port and published_port):
            raise ValidationError("the suggestion is incomplete")
        request = AdoptRequest(row.hostname, service, container_port, published_port)
        return self.adopt(project, environment, request, actor)


def _adopted(
    environment_id: int, found: DiscoveredDomain, checked: AdoptRequest, now: datetime
) -> Domain:
    record_type = DnsRecordType(found.record_type) if found.record_type else DnsRecordType.A
    target = found.record_content if found.record_type else None
    tls = found.vhost_ssl if found.vhost_ssl is not None else found.proxied
    return Domain(
        environment_id=environment_id,
        hostname=found.hostname,
        service=checked.service,
        container_port=checked.container_port,
        published_port=checked.published_port,
        dns_record_type=record_type,
        dns_target=validate_dns_target(record_type, target) if target else None,
        dns_proxied=found.proxied,
        tls_enabled=tls,
        status=DomainStatus.ACTIVE,
        origin=DomainOrigin.ADOPTED,
        adopted_at=now,
        vhost_file=found.vhost_file,
        cloudflare_zone_id=found.zone_id,
        cloudflare_record_id=found.record_id,
        created_at=now,
        updated_at=now,
    )
