"""Status checks of a domain: DNS points at the server, HTTPS works, the backend answers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from backend.adapters.errors import AdapterError
from backend.adapters.host_helper_client import HostHelper
from backend.adapters.probes import DnsResolver, HttpProber
from backend.config_ingress import IngressSettings
from backend.domain.clock import Clock, utcnow
from backend.domain.errors import NotFoundError
from backend.domain.ingress import DnsRecordType
from backend.hosthelper.names import WILDCARD_PREFIX
from backend.repositories.database import UnitOfWork
from backend.repositories.models import Domain, DomainCheck

WILDCARD_PROBE_LABEL = "ingress-check"
MAX_DETAIL = 300
CheckPart = dict[str, object]


@dataclass(frozen=True, slots=True)
class CheckResult:
    """The latest check of a domain."""

    domain_id: int
    checked_at: datetime
    ok: bool
    results: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class CheckProbes:
    """The outside-world observers a check needs."""

    resolver: DnsResolver
    prober: HttpProber
    helper: HostHelper


class DomainCheckService:
    """Runs and stores status checks; every result carries the time it was made."""

    def __init__(
        self,
        uow: UnitOfWork,
        probes: CheckProbes,
        settings: IngressSettings,
        clock: Clock = utcnow,
    ) -> None:
        self._uow = uow
        self._resolver = probes.resolver
        self._prober = probes.prober
        self._helper = probes.helper
        self._settings = settings
        self._clock = clock

    def run(self, domain_id: int) -> CheckResult:
        """Check DNS, HTTPS and the backend now and store the outcome."""
        with self._uow:
            domain = self._uow.domains.get(domain_id)
            if domain is None:
                raise NotFoundError(f"domain not found: {domain_id}")
        probe_name = _probe_name(domain.hostname)
        timeout = self._settings.check_timeout_seconds
        now = self._clock()
        dns = self._check_dns(domain, probe_name, now)
        https = self._check_http(domain, probe_name, timeout, now)
        backend = self._check_backend(domain, now)
        parts = {"dns": dns, "https": https, "backend": backend}
        ok = all(part["ok"] for part in parts.values())
        self._store(domain_id, now, ok, parts)
        return CheckResult(domain_id, now, ok, parts)

    def latest(self, domain_id: int) -> CheckResult | None:
        """Return the stored check, or ``None`` when none ran yet."""
        with self._uow:
            row = self._uow.domain_checks.get_for_domain(domain_id)
            if row is None:
                return None
            return CheckResult(domain_id, row.checked_at, row.ok, dict(row.results))

    # --- the three checks ---------------------------------------------------------------

    def _check_dns(self, domain: Domain, name: str, now: datetime) -> CheckPart:
        addresses = self._resolver.resolve(name)
        expected = self._expected_addresses(domain)
        if not addresses:
            return _part(False, "the name does not resolve", now, addresses=[])
        if domain.dns_proxied:
            return _part(True, "resolves (proxied by Cloudflare)", now, addresses=addresses)
        if domain.dns_record_type is DnsRecordType.CNAME or not expected:
            return _part(True, "resolves", now, addresses=addresses)
        matches = sorted(set(addresses) & expected)
        detail = "points at the server" if matches else "resolves to other addresses"
        return _part(bool(matches), detail, now, addresses=addresses, expected=sorted(expected))

    def _expected_addresses(self, domain: Domain) -> set[str]:
        configured = domain.dns_target or (
            self._settings.public_ipv4
            if domain.dns_record_type is DnsRecordType.A
            else self._settings.public_ipv6
        )
        return {configured} if configured else set()

    def _check_http(self, domain: Domain, name: str, timeout: float, now: datetime) -> CheckPart:
        result = self._prober.probe(name, tls=domain.tls_enabled, timeout=timeout)
        if not result.reached:
            return _part(False, result.detail or "no response", now)
        if domain.tls_enabled and not result.tls_valid:
            return _part(False, result.detail or "certificate is not valid", now)
        extra: CheckPart = {"status_code": result.status}
        if result.not_after is not None:
            extra["certificate_not_after"] = result.not_after.isoformat()
        ok = result.status is not None and result.status < 500
        return _part(ok, f"HTTP {result.status}", now, **extra)

    def _check_backend(self, domain: Domain, now: datetime) -> CheckPart:
        try:
            reachable = self._helper.check_backend(domain.published_port)
        except AdapterError as error:
            return _part(False, f"backend probe failed: {error}"[:MAX_DETAIL], now)
        detail = "accepts connections" if reachable else "nothing listens on the published port"
        return _part(reachable, detail, now, port=domain.published_port)

    def _store(self, domain_id: int, now: datetime, ok: bool, parts: dict[str, CheckPart]) -> None:
        with self._uow:
            row = self._uow.domain_checks.get_for_domain(domain_id)
            if row is None:
                row = self._uow.domain_checks.add(
                    DomainCheck(domain_id=domain_id, checked_at=now, ok=ok, results={})
                )
            row.checked_at, row.ok, row.results = now, ok, dict(parts)
            domain = self._uow.domains.get(domain_id)
            if domain is not None:
                domain.last_verified_at, domain.last_verified_ok = now, ok


def _probe_name(hostname: str) -> str:
    if hostname.startswith(WILDCARD_PREFIX):
        return f"{WILDCARD_PROBE_LABEL}.{hostname[len(WILDCARD_PREFIX) :]}"
    return hostname


def _part(ok: bool, detail: str, now: datetime, **extra: object) -> CheckPart:
    return {"ok": ok, "detail": detail[:MAX_DETAIL], "checked_at": now.isoformat(), **extra}
