"""Domain discovery (worker side, read-only): what Cloudflare and Apache already serve.

Cloudflare is read through its API (zones and A/AAAA/CNAME records, all pages), Apache through
the host helper's read-only ``vhost.discover`` when the helper is available, and the container
inventory tells which host port belongs to which project. The result is a snapshot in the
database that the Domains page reads; nothing here changes DNS, Apache or any project.

Cloudflare alone is enough: without the helper the page still lists the DNS records and says
that Apache routing needs the helper.
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from urllib.parse import urlsplit

from backend.adapters.cloudflare import CloudflareError, DnsRecord, Zone
from backend.adapters.host_helper_client import HostHelper, HostHelperError, VhostDiscovery
from backend.config_ingress import IngressSettings
from backend.domain.actor import Actor
from backend.domain.audit import AuditEntry, AuditTarget
from backend.domain.clock import Clock, utcnow
from backend.domain.discovery import (
    DiscoveredHost,
    DnsFact,
    IpSource,
    ProjectRef,
    PublishedPort,
    SourceState,
    VhostFact,
    is_cloudflare_edge,
    points_to_server,
    suggest,
)
from backend.domain.hostnames import Hostname
from backend.repositories.database import UnitOfWork
from backend.repositories.models import DiscoveredDomain, DiscoveryState
from backend.services.audit import add_audit_event

MAX_MESSAGE = 300
RECORD_PREFERENCE = {"A": 0, "AAAA": 1, "CNAME": 2}


class DnsCatalog(Protocol):
    """What discovery needs from Cloudflare (``CloudflareClient`` implements it)."""

    def list_zones(self) -> list[Zone]:
        """Every active zone."""

    def list_address_records(self, zone_id: str) -> list[DnsRecord]:
        """Every A, AAAA and CNAME record of a zone."""

    def find_zone(self, hostname: str) -> Zone:
        """The zone containing ``hostname``."""

    def list_records(self, zone_id: str, name: str) -> list[DnsRecord]:
        """All records with exactly this name."""


class PortCatalog(Protocol):
    """What discovery needs to know about projects (the worker implements it)."""

    def projects(self) -> list[ProjectRef]:
        """Every active project with its environments."""

    def published_ports(self) -> list[PublishedPort]:
        """Every container port published on the host by a project's compose services."""


Resolver = Callable[[str], list[str]]
TokenSource = Callable[[], str | None]
CatalogFactory = Callable[[str], DnsCatalog]


@dataclass(frozen=True, slots=True)
class DiscoveryPorts:
    """The outside world of discovery; every part is replaceable in tests."""

    token: TokenSource
    catalog: CatalogFactory
    helper: HostHelper
    ports: PortCatalog
    settings: Callable[[], IngressSettings]
    resolve: Resolver
    public_origin: str


@dataclass(frozen=True, slots=True)
class ZoneList:
    """The zones the token can see, or why there are none."""

    configured: bool
    zones: tuple[str, ...]
    message: str


@dataclass(frozen=True, slots=True)
class RecordView:
    """One existing DNS record of a looked-up name."""

    record_type: str
    content: str
    proxied: bool
    managed: bool


@dataclass(frozen=True, slots=True)
class NameLookup:
    """What Cloudflare already holds for a host name (the conflict warning of the forms)."""

    hostname: str
    configured: bool
    zone: str | None
    records: tuple[RecordView, ...]
    message: str


class DiscoveryService:
    """Builds the discovery snapshot and answers zone and name lookups."""

    def __init__(
        self,
        uow: UnitOfWork,
        ports: DiscoveryPorts,
        clock: Clock = utcnow,
    ) -> None:
        self._uow = uow
        self._ports = ports
        self._clock = clock

    # --- refresh ------------------------------------------------------------------------

    def refresh(self, actor: Actor) -> DiscoveryState:
        """Read Cloudflare and Apache again and replace the snapshot; returns the new state."""
        now = self._clock()
        state = DiscoveryState(refreshed_at=now)
        ips, source = self._server_ips()
        state.server_ip = ", ".join(sorted(ips)) or None
        state.ip_source = source.value
        dns, zones = self._read_cloudflare(state)
        vhosts = self._read_vhosts(state)
        state.zones = sorted(zone.name for zone in zones)
        if state.cloudflare == SourceState.ERROR.value:
            # A failed read must not wipe the previous snapshot: keep it and say what happened.
            return self._save(state, None, actor)
        hosts = self._combine(dns, vhosts, ips)
        zone_names = [zone.name for zone in zones]
        rows = [self._row(host, now, zone_names) for host in hosts]
        state.record_count = sum(1 for host in hosts if host.dns is not None)
        return self._save(state, rows, actor)

    def _save(
        self, state: DiscoveryState, rows: list[DiscoveredDomain] | None, actor: Actor
    ) -> DiscoveryState:
        with self._uow:
            if rows is not None:
                self._uow.discovery.replace_all(rows)
            saved = self._uow.discovery.save_state(state)
            details: dict[str, object] = {
                "hosts": len(rows) if rows is not None else 0,
                "zones": len(state.zones),
                "cloudflare": state.cloudflare,
                "helper": state.helper,
            }
            entry = AuditEntry(actor, "domain.discovery_refresh", AuditTarget("platform"), details)
            add_audit_event(self._uow, entry, self._clock)
            return saved

    def _server_ips(self) -> tuple[frozenset[str], IpSource]:
        settings = self._ports.settings()
        stored = {ip for ip in (settings.public_ipv4, settings.public_ipv6) if ip}
        if stored:
            return frozenset(stored), IpSource.SETTING
        host = urlsplit(self._ports.public_origin).hostname
        if host:
            found = {ip for ip in self._ports.resolve(host) if not is_cloudflare_edge(ip)}
            if found:
                return frozenset(found), IpSource.ORIGIN
        return frozenset(), IpSource.UNKNOWN

    def _read_cloudflare(self, state: DiscoveryState) -> tuple[list[DnsFact], list[Zone]]:
        token = self._ports.token()
        if token is None:
            state.cloudflare = SourceState.NO_TOKEN.value
            state.cloudflare_message = "no Cloudflare API token is stored"
            return [], []
        catalog = self._ports.catalog(token)
        try:
            zones = catalog.list_zones()
            facts = [
                _fact(zone, record)
                for zone in zones
                for record in catalog.list_address_records(zone.id)
            ]
        except CloudflareError as error:
            state.cloudflare = SourceState.ERROR.value
            state.cloudflare_message = str(error)[:MAX_MESSAGE]
            return [], []
        state.cloudflare = SourceState.OK.value
        state.cloudflare_message = None
        return facts, zones

    def _read_vhosts(self, state: DiscoveryState) -> list[VhostFact]:
        try:
            found: VhostDiscovery = self._ports.helper.discover_vhosts()
        except HostHelperError as error:
            outdated = error.code == "invalid_request"
            state.helper = (SourceState.OUTDATED if outdated else SourceState.MISSING).value
            state.helper_message = (
                "the installed host helper is too old to list Apache sites"
                if outdated
                else "the host helper is not reachable"
            )
            return []
        state.helper = SourceState.OK.value
        state.helper_message = (
            "some Apache sites were not listed (limit reached)" if found.truncated else None
        )
        return [
            VhostFact(
                entry.file,
                name,
                entry.upstream_host,
                entry.upstream_port,
                entry.ssl,
                entry.managed,
            )
            for entry in found.entries
            for name in entry.server_names
        ]

    # --- combining ------------------------------------------------------------------------

    def _combine(
        self,
        dns: Sequence[DnsFact],
        vhosts: Sequence[VhostFact],
        server_ips: frozenset[str],
    ) -> list[DiscoveredHost]:
        by_name: dict[str, list[DnsFact]] = {}
        for fact in dns:
            by_name.setdefault(fact.name, []).append(fact)
        sites = _merge_vhosts(vhosts)
        projects = self._ports.ports.projects()
        ports = self._ports.ports.published_ports()
        hosts: list[DiscoveredHost] = []
        for name in sorted(set(by_name) | set(sites)):
            record = _best_record(by_name.get(name, []), dns, server_ips)
            leads = points_to_server(record, dns, server_ips) if record else None
            site = sites.get(name)
            if record is not None and leads is False and site is None:
                continue  # a record of the zone that points somewhere else: not ours
            hosts.append(
                DiscoveredHost(name, record, site, leads, suggest(name, site, ports, projects))
            )
        return hosts

    def _row(
        self, host: DiscoveredHost, now: datetime, zone_names: Sequence[str]
    ) -> DiscoveredDomain:
        dns, site, hint = host.dns, host.vhost, host.suggestion
        return DiscoveredDomain(
            hostname=host.hostname,
            zone_name=dns.zone_name if dns else _zone_for(host.hostname, zone_names),
            zone_id=dns.zone_id if dns else None,
            record_id=dns.record_id if dns else None,
            record_type=dns.record_type if dns else None,
            record_content=dns.content[:255] if dns else None,
            proxied=dns.proxied if dns else False,
            points_to_server=host.points_to_server,
            vhost_file=site.file if site else None,
            vhost_managed=site.managed if site else False,
            upstream_host=site.upstream_host if site else None,
            upstream_port=site.upstream_port if site else None,
            vhost_ssl=site.ssl if site else None,
            suggested_project=hint.project if hint else None,
            suggested_environment=hint.environment if hint else None,
            suggested_service=hint.service if hint else None,
            suggested_container_port=hint.container_port if hint else None,
            suggested_published_port=hint.published_port if hint else None,
            suggestion_confidence=hint.confidence if hint else None,
            suggestion_reason=hint.reason[:300] if hint else None,
            seen_at=now,
        )

    # --- lookups --------------------------------------------------------------------------

    def zones(self) -> ZoneList:
        """The zones the stored token can see."""
        token = self._ports.token()
        if token is None:
            return ZoneList(False, (), "no Cloudflare API token is stored")
        try:
            zones = self._ports.catalog(token).list_zones()
        except CloudflareError as error:
            return ZoneList(True, (), str(error)[:MAX_MESSAGE])
        return ZoneList(True, tuple(sorted(zone.name for zone in zones)), "")

    def lookup(self, hostname: str) -> NameLookup:
        """The existing address records of ``hostname`` (read-only), for the conflict warning."""
        name = Hostname.parse(hostname, allow_wildcard=True).value
        token = self._ports.token()
        if token is None:
            return NameLookup(name, False, None, (), "no Cloudflare API token is stored")
        catalog = self._ports.catalog(token)
        try:
            zone = catalog.find_zone(name)
            records = [
                r
                for r in catalog.list_records(zone.id, name)
                if r.record_type in {"A", "AAAA", "CNAME"}
            ]
        except CloudflareError as error:
            return NameLookup(name, True, None, (), str(error)[:MAX_MESSAGE])
        views = tuple(RecordView(r.record_type, r.content, r.proxied, r.owned) for r in records)
        return NameLookup(name, True, zone.name, views, "")


def resolve_addresses(host: str) -> list[str]:
    """The addresses of ``host`` from the system resolver (empty when it does not resolve)."""
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return []
    return sorted({str(info[4][0]) for info in infos})


def _fact(zone: Zone, record: DnsRecord) -> DnsFact:
    return DnsFact(
        zone.id,
        zone.name,
        record.id,
        record.record_type,
        record.name,
        record.content,
        record.proxied,
        record.comment,
    )


def _best_record(
    own: Sequence[DnsFact], everything: Sequence[DnsFact], server_ips: frozenset[str]
) -> DnsFact | None:
    """The record of a name that shows best where it leads (an A, then AAAA, then CNAME)."""
    if not own:
        return None
    ranked = sorted(
        own,
        key=lambda item: (
            points_to_server(item, everything, server_ips) is not True,
            RECORD_PREFERENCE.get(item.record_type, 9),
        ),
    )
    return ranked[0]


def _merge_vhosts(vhosts: Sequence[VhostFact]) -> dict[str, VhostFact]:
    """One fact per name: the block that proxies wins; TLS is on when any block has it."""
    merged: dict[str, VhostFact] = {}
    for fact in vhosts:
        current = merged.get(fact.hostname)
        if current is None:
            merged[fact.hostname] = fact
            continue
        winner = (
            fact if current.upstream_port is None and fact.upstream_port is not None else current
        )
        merged[fact.hostname] = VhostFact(
            winner.file,
            winner.hostname,
            winner.upstream_host,
            winner.upstream_port,
            current.ssl or fact.ssl,
            winner.managed,
        )
    return merged


def _zone_for(hostname: str, zone_names: Sequence[str]) -> str | None:
    """The most specific known zone that contains ``hostname`` (for Apache-only names)."""
    bare = hostname.removeprefix("*.")
    inside = [name for name in zone_names if bare == name or bare.endswith("." + name)]
    return max(inside, key=len) if inside else None
