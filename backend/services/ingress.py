"""Preview, apply and remove the ingress of a domain: Cloudflare DNS, Apache vhost, TLS.

Nothing is changed by a preview. ``apply`` and ``remove`` recompute the plan and refuse to run
when it differs from the one the user reviewed (``plan_hash``), so the diff shown is the diff
applied. Calls are serialized: one ingress change at a time, matching the single helper.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from backend.adapters.cloudflare import CloudflareDns, DnsDesired, DnsPlan, DnsRecord
from backend.adapters.errors import AdapterError
from backend.adapters.host_helper_client import HostHelper, VhostProposal
from backend.config_ingress import IngressSettings
from backend.domain.actor import Actor
from backend.domain.clock import Clock, utcnow
from backend.domain.errors import ConfigurationError, ConflictError, DomainError, UpstreamError
from backend.domain.ingress import (
    MANAGED_TAG,
    DnsPlanView,
    DnsRecordType,
    DnsRecordView,
    DomainOrigin,
    DomainStatus,
    IngressPlanView,
    PlanAction,
    TlsPlanView,
    VhostPlanView,
)
from backend.repositories.models import Domain
from backend.services.certificates import CertificateService, CertificateStatus
from backend.services.domains import DomainService
from backend.services.ingress_lock import INGRESS_LOCK

DnsFactory = Callable[[Domain], CloudflareDns]
RENEWAL_MARGIN = timedelta(days=1)
MAX_MESSAGE = 400


@dataclass(frozen=True, slots=True)
class _Prepared:
    """A plan with the provider objects needed to execute it."""

    view: IngressPlanView
    dns: DnsPlan
    domain: Domain
    tls_final: bool
    needs_certificate: bool
    has_certificate: bool


@dataclass(frozen=True, slots=True)
class IngressPorts:
    """The external systems behind a hostname: Apache (via the helper) and Cloudflare."""

    helper: HostHelper
    dns_factory: DnsFactory


class IngressService:
    """Coordinates the three systems behind a hostname."""

    def __init__(
        self,
        domains: DomainService,
        certificates: CertificateService,
        ports: IngressPorts,
        settings: IngressSettings,
        clock: Clock = utcnow,
    ) -> None:
        self._domains = domains
        self._certificates = certificates
        self._helper = ports.helper
        self._dns_factory = ports.dns_factory
        self._settings = settings
        self._clock = clock

    # --- public use cases ---------------------------------------------------------------

    def preview(self, domain_id: int, operation: str = "apply") -> IngressPlanView:
        """Show what ``apply`` (or ``remove``) would change; contacts providers read-only."""
        domain = _require_managed(self._domains.get_by_id(domain_id))
        return self._prepare(domain, operation).view

    def apply(self, domain_id: int, plan_hash: str, actor: Actor) -> IngressPlanView:
        """Roll out DNS, vhost and certificate for a previously previewed plan."""
        with INGRESS_LOCK:
            domain = _require_managed(self._domains.get_by_id(domain_id))
            prepared = self._prepare(domain, "apply")
            self._require_reviewed(prepared, plan_hash)
            try:
                self._run_apply(prepared)
            except (AdapterError, DomainError) as error:
                self._fail(domain, actor, "apply", error)
            self._domains.audit(actor, "domain.apply", domain, _audit_details(prepared.view))
            return prepared.view

    def remove(self, domain_id: int, plan_hash: str, actor: Actor) -> IngressPlanView:
        """Take the domain out of Apache and DNS (own records only) and back to draft."""
        with INGRESS_LOCK:
            domain = _require_managed(self._domains.get_by_id(domain_id))
            prepared = self._prepare(domain, "remove")
            self._require_reviewed(prepared, plan_hash)
            try:
                self._run_remove(prepared)
            except (AdapterError, DomainError) as error:
                self._fail(domain, actor, "remove", error)
            self._domains.audit(actor, "domain.remove", domain, _audit_details(prepared.view))
            return prepared.view

    def issue_certificate(self, domain_id: int, actor: Actor) -> CertificateStatus:
        """Obtain (or renew) the certificate of an applied domain right now."""
        with INGRESS_LOCK:
            domain = _require_managed(self._domains.get_by_id(domain_id))
            if not domain.tls_enabled:
                raise ConflictError("TLS is disabled for this domain")
            if domain.status is not DomainStatus.ACTIVE:
                raise ConflictError("apply the domain before requesting a certificate")
            return self._certificates.issue(domain, actor)

    # --- planning -------------------------------------------------------------------------

    def _prepare(self, domain: Domain, operation: str) -> _Prepared:
        if operation not in {"apply", "remove"}:
            raise ConfigurationError(f"unknown ingress operation: {operation}")
        try:
            if operation == "apply":
                return self._prepare_apply(domain)
            return self._prepare_remove(domain)
        except AdapterError as error:
            raise UpstreamError(_message(error)) from error

    def _prepare_apply(self, domain: Domain) -> _Prepared:
        dns = self._dns_factory(domain)
        dns_plan = dns.plan_upsert(self._desired(domain))
        has_certificate = (
            self._certificates.has_valid_certificate(domain.id, margin=RENEWAL_MARGIN)
            and self._helper.certificate_info(domain.hostname) is not None
        )
        needs_certificate = domain.tls_enabled and not has_certificate
        tls_final = domain.tls_enabled
        addresses = self._listen_addresses()
        proposal = self._helper.render_vhost(
            domain.hostname, domain.published_port, tls=tls_final, listen_addresses=addresses
        )
        vhost = _vhost_view(proposal)
        tls = self._tls_view(domain, needs_certificate)
        steps = _apply_steps(dns_plan, vhost, tls, needs_certificate)
        view = _view(domain, "apply", _Parts(_dns_view(dns_plan), vhost, tls, steps))
        return _Prepared(view, dns_plan, domain, tls_final, needs_certificate, has_certificate)

    def _prepare_remove(self, domain: Domain) -> _Prepared:
        dns_plan = self._dns_factory(domain).plan_delete(domain.hostname)
        vhost = _remove_vhost_view(self._helper.get_vhost(domain.hostname))
        has_certificate = self._certificates.status_for(domain.id) is not None
        tls = TlsPlanView("remove" if has_certificate else "none")
        steps = _remove_steps(dns_plan, vhost, tls)
        view = _view(domain, "remove", _Parts(_dns_view(dns_plan), vhost, tls, steps))
        return _Prepared(view, dns_plan, domain, False, False, has_certificate)

    def _desired(self, domain: Domain) -> DnsDesired:
        record_type = domain.dns_record_type
        content = domain.dns_target
        if content is None:
            content = (
                self._settings.public_ipv4
                if record_type is DnsRecordType.A
                else self._settings.public_ipv6
            )
        if content is None:
            raise ConfigurationError(
                f"no server address configured for {record_type.value} records "
                "(set JENDOCK_INGRESS_PUBLIC_IPV4/IPV6 or the domain's DNS target)"
            )
        comment = f"{MANAGED_TAG} domain={domain.id}"
        return DnsDesired(record_type, domain.hostname, content, domain.dns_proxied, comment)

    def _listen_addresses(self) -> tuple[str, ...]:
        return tuple(
            address
            for address in (self._settings.public_ipv4, self._settings.public_ipv6)
            if address is not None
        )

    def _tls_view(self, domain: Domain, needs_certificate: bool) -> TlsPlanView:
        status = self._certificates.status_for(domain.id)
        if not domain.tls_enabled:
            return TlsPlanView("remove" if status else "none")
        if needs_certificate:
            return TlsPlanView("issue", challenge=self._settings.acme_challenge)
        return TlsPlanView("keep", not_after=status.not_after if status else None)

    def _require_reviewed(self, prepared: _Prepared, plan_hash: str) -> None:
        if prepared.view.blocked:
            raise ConflictError(prepared.view.dns.message)
        if prepared.view.plan_hash != plan_hash:
            raise ConflictError("the plan changed since the preview; review it again")

    # --- execution ------------------------------------------------------------------------

    def _run_apply(self, prepared: _Prepared) -> None:
        domain = prepared.domain
        addresses = self._listen_addresses()
        dns = self._dns_factory(domain)
        record = dns.apply_upsert(prepared.dns)
        if prepared.needs_certificate:
            # Reachable over HTTP first (needed by HTTP-01, harmless for DNS-01), then TLS.
            self._helper.put_vhost(
                domain.hostname, domain.published_port, tls=False, listen_addresses=addresses
            )
            self._certificates.issue(domain, Actor.system("ingress"))
        self._helper.put_vhost(
            domain.hostname,
            domain.published_port,
            tls=prepared.tls_final,
            listen_addresses=addresses,
        )
        if not domain.tls_enabled and prepared.has_certificate:
            self._certificates.discard(domain)
        self._domains.record_applied(
            domain.id, zone_id=prepared.dns.zone.id, record_id=record.id if record else None
        )

    def _run_remove(self, prepared: _Prepared) -> None:
        domain = prepared.domain
        self._helper.remove_vhost(domain.hostname)
        self._certificates.discard(domain)
        self._dns_factory(domain).apply_delete(prepared.dns)
        self._domains.record_removed(domain.id)

    def _fail(self, domain: Domain, actor: Actor, operation: str, error: Exception) -> None:
        message = _message(error)
        self._domains.record_error(domain.id, message)
        self._domains.audit(
            actor,
            f"domain.{operation}_failed",
            domain,
            {"hostname": domain.hostname, "error": message},
        )
        if isinstance(error, DomainError):
            raise error
        raise UpstreamError(message) from error


def _require_managed(domain: Domain) -> Domain:
    """Adopted domains exist outside Jendock: it never plans, applies or removes them."""
    if domain.origin is DomainOrigin.ADOPTED:
        raise ConflictError(
            f"{domain.hostname} was adopted: its DNS record and Apache site are managed outside "
            "Jendock, so Jendock does not change them"
        )
    return domain


def _message(error: Exception) -> str:
    return str(error)[:MAX_MESSAGE]


def _record_view(record: DnsRecord) -> DnsRecordView:
    return DnsRecordView(record.record_type, record.name, record.content, record.proxied)


def _dns_view(plan: DnsPlan) -> DnsPlanView:
    desired = None
    if plan.desired is not None:
        d = plan.desired
        desired = DnsRecordView(d.record_type.value, d.name, d.content, d.proxied)
    current = _record_view(plan.existing) if plan.existing else None
    removes = tuple(_record_view(r) for r in plan.stale)
    return DnsPlanView(plan.action, plan.zone.name, current, desired, removes, plan.message)


def _diff(current: str | None, proposed: str | None) -> str:
    lines = difflib.unified_diff(
        (current or "").splitlines(),
        (proposed or "").splitlines(),
        fromfile="current",
        tofile="proposed",
        lineterm="",
    )
    return "\n".join(lines)


def _vhost_view(proposal: VhostProposal) -> VhostPlanView:
    if proposal.current is None:
        action = PlanAction.CREATE
    elif proposal.current == proposal.proposed:
        action = PlanAction.NOOP
    else:
        action = PlanAction.UPDATE
    return VhostPlanView(
        action, proposal.current, proposal.proposed, _diff(proposal.current, proposal.proposed)
    )


def _remove_vhost_view(current: str | None) -> VhostPlanView:
    action = PlanAction.DELETE if current is not None else PlanAction.NOOP
    return VhostPlanView(action, current, None, _diff(current, None))


def _apply_steps(
    dns: DnsPlan, vhost: VhostPlanView, tls: TlsPlanView, needs_certificate: bool
) -> tuple[str, ...]:
    steps = [f"DNS: {dns.action.value}"]
    if needs_certificate:
        steps.append("Apache: HTTP vhost, then reload")
        steps.append(f"TLS: issue certificate ({tls.challenge})")
    steps.append(f"Apache: vhost {vhost.action.value}, configtest, graceful reload")
    if tls.action == "remove":
        steps.append("TLS: remove certificate")
    return tuple(steps)


def _remove_steps(dns: DnsPlan, vhost: VhostPlanView, tls: TlsPlanView) -> tuple[str, ...]:
    return (
        f"Apache: vhost {vhost.action.value}, configtest, graceful reload",
        f"TLS: {tls.action}",
        f"DNS: {dns.action.value} (only records created by Jendock)",
    )


@dataclass(frozen=True, slots=True)
class _Parts:
    dns: DnsPlanView
    vhost: VhostPlanView
    tls: TlsPlanView
    steps: tuple[str, ...]


def _view(domain: Domain, operation: str, parts: _Parts) -> IngressPlanView:
    dns, vhost, tls = parts.dns, parts.vhost, parts.tls
    material = {
        "domain": domain.id,
        "operation": operation,
        "dns": [
            dns.action.value,
            dns.zone,
            repr(dns.current),
            repr(dns.desired),
            [repr(r) for r in dns.removes],
        ],
        "vhost": [vhost.current, vhost.proposed],
        "tls": [tls.action, tls.challenge],
    }
    digest = hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
    return IngressPlanView(
        domain.id, domain.hostname, operation, dns, vhost, tls, parts.steps, digest
    )


def _audit_details(view: IngressPlanView) -> dict[str, object]:
    return {
        "hostname": view.hostname,
        "dns": view.dns.action.value,
        "vhost": view.vhost.action.value,
        "tls": view.tls.action,
        "plan": view.plan_hash[:16],
    }
