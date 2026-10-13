"""Domain discovery: what was found on Cloudflare and Apache, and how it is matched to projects.

Pure data and rules, no I/O. The worker gathers the facts (``backend.services.discovery``);
``suggest`` turns them into a project/environment/service suggestion with a confidence and a
plain-language reason, so a person (or "adopt all suggested") can accept it without guessing.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum

from backend.domain.enums import EnvironmentName

LOCAL_UPSTREAM_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"})
DEV_WORDS = frozenset({"dev", "develop", "development", "staging", "stage", "stg", "test", "qa"})
MIN_TOKEN = 3
MAX_CNAME_DEPTH = 5
# Cloudflare's published edge ranges (https://www.cloudflare.com/ips/): a proxied record's own
# hostname resolves to these, which is never the origin server's address.
_EDGE_NETWORKS = tuple(
    ipaddress.ip_network(text)
    for text in (
        "173.245.48.0/20",
        "103.21.244.0/22",
        "103.22.200.0/22",
        "103.31.4.0/22",
        "141.101.64.0/18",
        "108.162.192.0/18",
        "190.93.240.0/20",
        "188.114.96.0/20",
        "197.234.240.0/22",
        "198.41.128.0/17",
        "162.158.0.0/15",
        "104.16.0.0/13",
        "104.24.0.0/14",
        "172.64.0.0/13",
        "131.0.72.0/22",
        "2400:cb00::/32",
        "2606:4700::/32",
        "2803:f800::/32",
        "2405:b500::/32",
        "2405:8100::/32",
        "2a06:98c0::/29",
        "2c0f:f248::/32",
    )
)


class Confidence(StrEnum):
    """How sure a suggestion is."""

    HIGH = "high"  # the Apache upstream port is a project's published container port
    MEDIUM = "medium"  # the host name carries the project's slug
    LOW = "low"  # the host name only shares a word with the project


class SourceState(StrEnum):
    """Whether a source of discovery (Cloudflare, the host helper) could be read."""

    OK = "ok"
    NO_TOKEN = "no_token"  # noqa: S105 - a state name, not a credential; Cloudflare: no API token is stored yet
    ERROR = "error"  # Cloudflare: the token was refused or Cloudflare did not answer
    MISSING = "missing"  # helper: not installed or not reachable
    OUTDATED = "outdated"  # helper: reachable but too old to list Apache sites
    UNKNOWN = "unknown"  # no refresh has run yet


class IpSource(StrEnum):
    """Where the server's public address came from."""

    SETTING = "setting"  # Settings > Integrations, or JENDOCK_INGRESS_PUBLIC_IPV4/IPV6
    ORIGIN = "origin"  # resolved from the public origin of Jendock itself
    UNKNOWN = "unknown"


class LinkFilter(StrEnum):
    """The "linked / unlinked / suggested" filter of the Domains page."""

    LINKED = "linked"
    UNLINKED = "unlinked"
    SUGGESTED = "suggested"


@dataclass(frozen=True, slots=True)
class PublishedPort:
    """A container port published on the host, as the worker sees it."""

    project: str
    environment: EnvironmentName
    service: str
    container_port: int
    host_port: int


@dataclass(frozen=True, slots=True)
class ProjectRef:
    """A project as the matcher needs it: slug, name and the environments it has."""

    slug: str
    name: str
    environments: tuple[EnvironmentName, ...]


@dataclass(frozen=True, slots=True)
class DnsFact:
    """One address record found on Cloudflare."""

    zone_id: str
    zone_name: str
    record_id: str
    record_type: str
    name: str
    content: str
    proxied: bool
    comment: str | None = None


@dataclass(frozen=True, slots=True)
class VhostFact:
    """What one Apache virtual host says about a host name."""

    file: str
    hostname: str
    upstream_host: str | None
    upstream_port: int | None
    ssl: bool
    managed: bool = False

    @property
    def local_upstream(self) -> bool:
        """Whether the vhost proxies to this machine (the only upstreams that can be matched)."""
        return self.upstream_host is not None and self.upstream_host in LOCAL_UPSTREAM_HOSTS


@dataclass(frozen=True, slots=True)
class Suggestion:
    """Where a discovered host name probably belongs."""

    project: str
    environment: EnvironmentName
    service: str | None
    container_port: int | None
    published_port: int | None
    confidence: Confidence
    reason: str

    @property
    def complete(self) -> bool:
        """Whether service and both ports are known, so it can be adopted without asking."""
        return None not in (self.service, self.container_port, self.published_port)


@dataclass(frozen=True, slots=True)
class DiscoveredHost:
    """One host name with everything known about it (the row of the Domains page)."""

    hostname: str
    dns: DnsFact | None
    vhost: VhostFact | None
    points_to_server: bool | None
    suggestion: Suggestion | None = None


def is_cloudflare_edge(address: str) -> bool:
    """Whether ``address`` belongs to Cloudflare's proxy network."""
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(parsed in network for network in _EDGE_NETWORKS if network.version == parsed.version)


def points_to_server(
    record: DnsFact, records: Sequence[DnsFact], server_ips: frozenset[str]
) -> bool | None:
    """Whether a record leads to the server: an address that is one of ``server_ips``, or a
    CNAME whose chain (followed through ``records``) ends in one. ``None`` when the server's
    address is not known, so nothing can be said."""
    if not server_ips:
        return None
    current, seen = record, set[str]()
    for _ in range(MAX_CNAME_DEPTH):
        if current.record_type != "CNAME":
            return current.content in server_ips
        target = current.content.lower().rstrip(".")
        if target in seen:
            return False
        seen.add(target)
        following = next((item for item in records if item.name == target), None)
        if following is None:
            return False
        current = following
    return False


def _tokens(text: str) -> set[str]:
    return {part for part in re.split(r"[^a-z0-9]+", text.lower()) if part}


def _name_score(hostname: str, project: ProjectRef) -> Confidence | None:
    """MEDIUM when the host name contains the project's slug as a whole label or label
    sequence (``shop-api.example.com`` for ``shop-api``); LOW when it shares a word."""
    labels = hostname.lower().split(".")[:-2] or hostname.lower().split(".")[:1]
    flat = "-".join(labels)
    slug = project.slug.lower()
    whole = re.search(rf"(?<![a-z0-9]){re.escape(slug)}(?![a-z0-9])", flat)
    if len(slug) >= MIN_TOKEN and whole is not None:
        return Confidence.MEDIUM
    host_tokens = _tokens(".".join(labels)) - DEV_WORDS
    project_tokens = {
        t for t in _tokens(project.slug) | _tokens(project.name) if len(t) >= MIN_TOKEN
    }
    return Confidence.LOW if host_tokens & project_tokens else None


def _guess_environment(hostname: str, project: ProjectRef) -> EnvironmentName:
    wanted = (
        EnvironmentName.DEVELOPMENT
        if _tokens(hostname.split(".", maxsplit=1)[0]) & DEV_WORDS
        else EnvironmentName.PRODUCTION
    )
    if wanted in project.environments or not project.environments:
        return wanted
    return project.environments[0]


def suggest(
    hostname: str,
    vhost: VhostFact | None,
    ports: Iterable[PublishedPort],
    projects: Sequence[ProjectRef],
) -> Suggestion | None:
    """The best guess for where ``hostname`` belongs, or ``None``.

    1. The vhost's local upstream port is the published port of exactly one container: that is
       the container it serves (HIGH; the host name matching the project's slug is only
       mentioned, because a published port is unique on the machine).
    2. Otherwise the host name is compared with the project slugs and names (MEDIUM or LOW).
       The service and ports stay open: only Apache could tell them and it says nothing.
    """
    if vhost is not None and vhost.local_upstream and vhost.upstream_port is not None:
        matches = [item for item in ports if item.host_port == vhost.upstream_port]
        found = {(item.project, item.environment, item.service) for item in matches}
        if len(found) == 1:
            item = matches[0]
            where = f"{item.service} of {item.project}/{item.environment.value}"
            return Suggestion(
                item.project,
                item.environment,
                item.service,
                item.container_port,
                item.host_port,
                Confidence.HIGH,
                f"Apache proxies to port {item.host_port}, the published port of {where}",
            )
    return _suggest_by_name(hostname, vhost, projects)


def _suggest_by_name(
    hostname: str, vhost: VhostFact | None, projects: Sequence[ProjectRef]
) -> Suggestion | None:
    scored = [(project, _name_score(hostname, project)) for project in projects]
    candidates = [(project, score) for project, score in scored if score is not None]
    if not candidates:
        return None
    best = (
        Confidence.MEDIUM if any(s is Confidence.MEDIUM for _, s in candidates) else Confidence.LOW
    )
    top = [project for project, score in candidates if score is best]
    if len(top) != 1:
        return None  # ambiguous: better no suggestion than a wrong one
    project = top[0]
    port = vhost.upstream_port if vhost is not None and vhost.local_upstream else None
    how = "contains the project's name" if best is Confidence.MEDIUM else "shares a word with it"
    return Suggestion(
        project.slug,
        _guess_environment(hostname, project),
        None,
        None,
        port,
        best,
        f"The host name {how} ({project.slug}); check the service and port before adopting",
    )
