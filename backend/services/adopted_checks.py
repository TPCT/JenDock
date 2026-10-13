"""Status of an adopted domain: is the DNS record there, is the Apache site there, does it answer.

Everything here is a read: Cloudflare is only listed, the host helper only lists sites and probes
a loopback port, and the public check is a single ``HEAD`` request with a timeout. The parts are
stored like any other check, so the project's Domains tab shows them the same way. A part that
cannot be checked (no token, no helper) is marked ``skipped`` and does not fail the check.
"""

from __future__ import annotations

from datetime import datetime

from backend.adapters.cloudflare import CloudflareError, DnsRecord
from backend.adapters.errors import AdapterError
from backend.adapters.host_helper_client import HostHelperError, VhostEntry
from backend.adapters.probes import HttpProber
from backend.domain.clock import Clock, utcnow
from backend.domain.errors import ConflictError, NotFoundError
from backend.domain.ingress import DomainOrigin
from backend.hosthelper.names import WILDCARD_PREFIX
from backend.repositories.database import UnitOfWork
from backend.repositories.models import Domain
from backend.services.discovery import DiscoveryPorts
from backend.services.domain_checks import CheckPart, CheckResult, check_part, store_check

ADDRESS_TYPES = frozenset({"A", "AAAA", "CNAME"})
HELPER_HINT = "Install the host helper to see Apache routing"
SERVER_ERROR = 500


class AdoptedCheckService:
    """Runs and stores the checks of adopted domains."""

    def __init__(
        self,
        uow: UnitOfWork,
        ports: DiscoveryPorts,
        prober: HttpProber,
        clock: Clock = utcnow,
    ) -> None:
        self._uow = uow
        self._ports = ports
        self._prober = prober
        self._clock = clock

    def run(self, domain_id: int) -> CheckResult:
        """Check DNS, the Apache site, HTTPS and the backend port now and store the outcome."""
        with self._uow:
            domain = self._uow.domains.get(domain_id)
            if domain is None:
                raise NotFoundError(f"domain not found: {domain_id}")
            if domain.origin is not DomainOrigin.ADOPTED:
                raise ConflictError("only adopted domains are checked this way")
        now = self._clock()
        sites = self._sites()
        parts: dict[str, CheckPart] = {
            "dns": self._dns(domain, now),
            "vhost": _vhost_part(domain.hostname, sites, now),
            "https": self._https(domain, now),
            "backend": self._backend(domain, sites is not None, now),
        }
        ok = all(bool(part["ok"]) or bool(part.get("skipped")) for part in parts.values())
        store_check(self._uow, domain_id, now, ok, parts)
        return CheckResult(domain_id, now, ok, parts)

    def _sites(self) -> list[VhostEntry] | None:
        try:
            return list(self._ports.helper.discover_vhosts().entries)
        except HostHelperError:
            return None

    def _dns(self, domain: Domain, now: datetime) -> CheckPart:
        token = self._ports.token()
        if token is None:
            addresses = self._ports.resolve(domain.hostname)
            detail = "resolves" if addresses else "the name does not resolve"
            note = f"{detail} (connect Cloudflare to see the record itself)"
            return check_part(bool(addresses), note, now, addresses=addresses)
        catalog = self._ports.catalog(token)
        try:
            zone = catalog.find_zone(domain.hostname)
            found = catalog.list_records(zone.id, domain.hostname)
        except CloudflareError as error:
            return check_part(False, f"Cloudflare could not be read: {error}", now)
        return _dns_part([r for r in found if r.record_type in ADDRESS_TYPES], now)

    def _https(self, domain: Domain, now: datetime) -> CheckPart:
        timeout = self._ports.settings().check_timeout_seconds
        result = self._prober.probe(
            _probe_name(domain.hostname), tls=domain.tls_enabled, timeout=timeout
        )
        scheme = "HTTPS" if domain.tls_enabled else "HTTP"
        if not result.reached:
            return check_part(False, result.detail or f"{scheme} did not answer", now)
        if domain.tls_enabled and not result.tls_valid:
            return check_part(False, result.detail or "the certificate is not valid", now)
        extra: CheckPart = {"status_code": result.status}
        if result.not_after is not None:
            extra["certificate_not_after"] = result.not_after.isoformat()
        ok = result.status is not None and result.status < SERVER_ERROR
        return check_part(ok, f"{scheme} answered with {result.status}", now, **extra)

    def _backend(self, domain: Domain, helper_up: bool, now: datetime) -> CheckPart:
        if not helper_up:
            return check_part(True, HELPER_HINT, now, skipped=True)
        try:
            reachable = self._ports.helper.check_backend(domain.published_port)
        except (HostHelperError, AdapterError) as error:
            code = getattr(error, "code", "")
            detail = (
                "the host helper only probes ports in its allowed range"
                if code == "invalid_request"
                else HELPER_HINT
            )
            return check_part(True, detail, now, skipped=True)
        detail = "accepts connections" if reachable else "nothing listens on the published port"
        return check_part(reachable, detail, now, port=domain.published_port)


def _probe_name(hostname: str) -> str:
    if hostname.startswith(WILDCARD_PREFIX):
        return f"ingress-check.{hostname[len(WILDCARD_PREFIX) :]}"
    return hostname


def _dns_part(records: list[DnsRecord], now: datetime) -> CheckPart:
    if not records:
        return check_part(False, "no DNS record found for this name in Cloudflare", now)
    best = sorted(records, key=lambda r: r.record_type)[0]
    mode = "proxied by Cloudflare" if best.proxied else "DNS only (not proxied)"
    return check_part(
        True,
        f"{best.record_type} record found: {best.content}, {mode}",
        now,
        record_type=best.record_type,
        content=best.content,
        proxied=best.proxied,
    )


def _vhost_part(hostname: str, sites: list[VhostEntry] | None, now: datetime) -> CheckPart:
    if sites is None:
        return check_part(True, HELPER_HINT, now, skipped=True)
    for site in sites:
        if hostname in site.server_names:
            target = (
                f"{site.upstream_host}:{site.upstream_port}" if site.upstream_port else "no proxy"
            )
            tls = ", HTTPS" if site.ssl else ""
            return check_part(
                True,
                f"Apache site {site.file} found, proxies to {target}{tls}",
                now,
                file=site.file,
                upstream_port=site.upstream_port,
                ssl=site.ssl,
            )
    return check_part(False, "no Apache site answers to this name", now)
