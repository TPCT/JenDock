"""Domain use cases: definition, lookup and state transitions (no DNS, Apache or TLS here)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from backend.domain.actor import Actor
from backend.domain.audit import AuditEntry, AuditTarget
from backend.domain.clock import Clock, utcnow
from backend.domain.enums import EnvironmentName
from backend.domain.errors import ConflictError, NotFoundError, ValidationError
from backend.domain.hostnames import Hostname
from backend.domain.ingress import (
    DnsRecordType,
    DomainStatus,
    validate_dns_target,
    validate_port,
)
from backend.domain.values import validate_service_names
from backend.repositories.database import UnitOfWork
from backend.repositories.models import Domain, Environment
from backend.services.audit import add_audit_event
from backend.services.lookup import require_environment

MAX_MESSAGE_LENGTH = 500


@dataclass(frozen=True, slots=True)
class DomainDraft:
    """User-supplied definition of a domain; validated by ``DomainService``."""

    hostname: str
    service: str
    container_port: int
    published_port: int
    dns_record_type: DnsRecordType = DnsRecordType.A
    dns_target: str | None = None
    dns_proxied: bool = False
    tls_enabled: bool = True


@dataclass(frozen=True, slots=True)
class DomainPatch:
    """Fields of a domain that may change; ``None`` keeps the current value.

    ``clear_dns_target`` resets the target to the server default (``None`` cannot mean both).
    """

    service: str | None = None
    container_port: int | None = None
    published_port: int | None = None
    dns_record_type: DnsRecordType | None = None
    dns_target: str | None = None
    clear_dns_target: bool = False
    dns_proxied: bool | None = None
    tls_enabled: bool | None = None


class DomainService:
    """Defines domains per environment and tracks their state."""

    def __init__(
        self, uow: UnitOfWork, clock: Clock = utcnow, *, allow_wildcards: bool = False
    ) -> None:
        self._uow = uow
        self._clock = clock
        self._allow_wildcards = allow_wildcards

    def create(self, slug: str, env: EnvironmentName, draft: DomainDraft, actor: Actor) -> Domain:
        """Define a new domain in ``draft`` status; hostnames are unique across all projects."""
        hostname = Hostname.parse(draft.hostname, allow_wildcard=self._allow_wildcards).value
        checked = _checked_fields(draft)
        with self._uow:
            environment = require_environment(self._uow, slug, env)
            _check_service(environment, checked.service)
            if self._uow.domains.get_by_hostname(hostname) is not None:
                raise ConflictError(f"hostname is already in use: {hostname}")
            now = self._clock()
            domain = self._uow.domains.add(
                Domain(
                    environment_id=environment.id,
                    hostname=hostname,
                    service=checked.service,
                    container_port=checked.container_port,
                    published_port=checked.published_port,
                    dns_record_type=checked.dns_record_type,
                    dns_target=checked.dns_target,
                    dns_proxied=checked.dns_proxied,
                    tls_enabled=checked.tls_enabled,
                    status=DomainStatus.DRAFT,
                    created_at=now,
                    updated_at=now,
                )
            )
            self._audit(actor, "domain.create", domain, {"hostname": hostname})
            return domain

    def update(
        self, slug: str, env: EnvironmentName, domain_id: int, patch: DomainPatch, actor: Actor
    ) -> Domain:
        """Change the routing of a domain; run preview and apply again to roll it out."""
        with self._uow:
            domain = self._require(slug, env, domain_id)
            environment = require_environment(self._uow, slug, env)
            merged = _merge(domain, patch)
            checked = _checked_fields(merged)
            _check_service(environment, checked.service)
            changed = _apply_fields(domain, checked)
            domain.updated_at = self._clock()
            self._audit(
                actor, "domain.update", domain, {"hostname": domain.hostname, "fields": changed}
            )
            return domain

    def get(self, slug: str, env: EnvironmentName, domain_id: int) -> Domain:
        """Return one domain of the environment."""
        with self._uow:
            return self._require(slug, env, domain_id)

    def list(self, slug: str, env: EnvironmentName) -> Sequence[Domain]:
        """Return the domains of an environment ordered by hostname."""
        with self._uow:
            environment = require_environment(self._uow, slug, env)
            return self._uow.domains.list_for_environment(environment.id)

    def delete(self, slug: str, env: EnvironmentName, domain_id: int, actor: Actor) -> None:
        """Delete a domain that is not applied anywhere (remove it from DNS/Apache first)."""
        with self._uow:
            domain = self._require(slug, env, domain_id)
            if domain.status is not DomainStatus.DRAFT:
                raise ConflictError("remove the domain from DNS and Apache before deleting it")
            self._audit(actor, "domain.delete", domain, {"hostname": domain.hostname})
            self._uow.domains.delete(domain)

    # --- used by the worker's orchestration -----------------------------------------------

    def get_by_id(self, domain_id: int) -> Domain:
        """Return a domain by id (no environment check; for trusted internal callers)."""
        with self._uow:
            domain = self._uow.domains.get(domain_id)
            if domain is None:
                raise NotFoundError(f"domain not found: {domain_id}")
            return domain

    def record_applied(
        self,
        domain_id: int,
        *,
        zone_id: str | None,
        record_id: str | None,
    ) -> Domain:
        """Mark the domain active and remember the Cloudflare ids of its record."""
        with self._uow:
            domain = self._require_by_id(domain_id)
            domain.status = DomainStatus.ACTIVE
            domain.status_message = None
            domain.cloudflare_zone_id = zone_id
            domain.cloudflare_record_id = record_id
            domain.applied_at = domain.updated_at = self._clock()
            return domain

    def record_error(self, domain_id: int, message: str) -> Domain:
        """Mark the last apply as failed with a short, secret-free message."""
        with self._uow:
            domain = self._require_by_id(domain_id)
            domain.status = DomainStatus.ERROR
            domain.status_message = message[:MAX_MESSAGE_LENGTH]
            domain.updated_at = self._clock()
            return domain

    def record_removed(self, domain_id: int) -> Domain:
        """Return the domain to ``draft`` after DNS, vhost and certificate were removed."""
        with self._uow:
            domain = self._require_by_id(domain_id)
            domain.status = DomainStatus.DRAFT
            domain.status_message = None
            domain.cloudflare_zone_id = domain.cloudflare_record_id = None
            domain.applied_at = None
            domain.updated_at = self._clock()
            return domain

    def audit(self, actor: Actor, action: str, domain: Domain, details: dict[str, object]) -> None:
        """Write one audit event for ``domain`` in its own transaction."""
        with self._uow:
            self._audit(actor, action, domain, details)

    # --- internals ---------------------------------------------------------------------------

    def _require(self, slug: str, env: EnvironmentName, domain_id: int) -> Domain:
        environment = require_environment(self._uow, slug, env)
        domain = self._uow.domains.get(domain_id)
        if domain is None or domain.environment_id != environment.id:
            raise NotFoundError(f"domain not found: {domain_id}")
        return domain

    def _require_by_id(self, domain_id: int) -> Domain:
        domain = self._uow.domains.get(domain_id)
        if domain is None:
            raise NotFoundError(f"domain not found: {domain_id}")
        return domain

    def _audit(self, actor: Actor, action: str, domain: Domain, details: dict[str, object]) -> None:
        target = AuditTarget("domain", str(domain.id))
        add_audit_event(self._uow, AuditEntry(actor, action, target, details), self._clock)


def _merge(domain: Domain, patch: DomainPatch) -> DomainDraft:
    draft = DomainDraft(
        hostname=domain.hostname,
        service=domain.service,
        container_port=domain.container_port,
        published_port=domain.published_port,
        dns_record_type=domain.dns_record_type,
        dns_target=domain.dns_target,
        dns_proxied=domain.dns_proxied,
        tls_enabled=domain.tls_enabled,
    )
    updates: dict[str, object] = {
        key: value
        for key in (
            "service",
            "container_port",
            "published_port",
            "dns_record_type",
            "dns_proxied",
            "tls_enabled",
        )
        if (value := getattr(patch, key)) is not None
    }
    if patch.dns_target is not None:
        updates["dns_target"] = patch.dns_target
    if patch.clear_dns_target:
        updates["dns_target"] = None
    return replace(draft, **updates)  # type: ignore[arg-type]


def _checked_fields(draft: DomainDraft) -> DomainDraft:
    """Validate the routing fields of a draft and return the normalized draft."""
    service = validate_service_names([draft.service])[0]
    if len(service) > 63:
        raise ValidationError("service name is too long")
    return replace(
        draft,
        service=service,
        container_port=validate_port(draft.container_port, "container_port"),
        published_port=validate_port(draft.published_port, "published_port", minimum=1024),
        dns_target=validate_dns_target(draft.dns_record_type, draft.dns_target),
    )


def _check_service(environment: Environment, service: str) -> None:
    if environment.services and service not in environment.services:
        raise ValidationError(f"service is not part of the environment: {service}")


def _apply_fields(domain: Domain, draft: DomainDraft) -> list[str]:
    changed = []
    for name in (
        "service",
        "container_port",
        "published_port",
        "dns_record_type",
        "dns_target",
        "dns_proxied",
        "tls_enabled",
    ):
        value = getattr(draft, name)
        if getattr(domain, name) != value:
            setattr(domain, name, value)
            changed.append(name)
    return changed
