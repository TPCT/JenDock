"""Read model of the discovery snapshot for the Domains page and the MCP tools (API side)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from backend.domain.discovery import IpSource, LinkFilter, SourceState
from backend.domain.enums import EnvironmentName
from backend.domain.ingress import DomainOrigin
from backend.domain.paging import Page, PageQuery
from backend.repositories.database import UnitOfWork
from backend.repositories.models import DiscoveredDomain, Domain


@dataclass(frozen=True, slots=True)
class LinkView:
    """The Jendock domain a discovered host name is linked to."""

    domain_id: int
    project: str
    environment: EnvironmentName
    origin: DomainOrigin


@dataclass(frozen=True, slots=True)
class DiscoveredView:
    """One discovered host name with its link, if any."""

    row: DiscoveredDomain
    link: LinkView | None


@dataclass(frozen=True, slots=True)
class DiscoveryStateView:
    """What the last refresh could read, and the zones and counts the page's filters need."""

    refreshed_at: datetime | None
    cloudflare: SourceState
    cloudflare_message: str | None
    helper: SourceState
    helper_message: str | None
    server_ip: str | None
    ip_source: IpSource
    zones: tuple[str, ...]
    hosts: int
    linked: int
    suggested: int
    adoptable: int


class DiscoveryQueryService:
    """Pages and states; it never contacts Cloudflare or Apache."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    def state(self) -> DiscoveryStateView:
        """The state of the last refresh (everything ``unknown`` before the first one)."""
        with self._uow:
            row = self._uow.discovery.get_state()
            repo = self._uow.discovery
            everything = repo.list_all()
            linked = {d.hostname for d in self._uow.domains.list_all()}
            hosts = len(everything)
            joined = sum(1 for item in everything if item.hostname in linked)
            suggested = sum(
                1 for item in everything if item.hostname not in linked and item.suggested_project
            )
            adoptable = len(repo.list_obvious_unlinked())
            zones = tuple(repo.zones())
        if row is None:
            return DiscoveryStateView(
                None,
                SourceState.UNKNOWN,
                None,
                SourceState.UNKNOWN,
                None,
                None,
                IpSource.UNKNOWN,
                zones,
                hosts,
                joined,
                suggested,
                adoptable,
            )
        return DiscoveryStateView(
            row.refreshed_at,
            SourceState(row.cloudflare),
            row.cloudflare_message,
            SourceState(row.helper),
            row.helper_message,
            row.server_ip,
            IpSource(row.ip_source),
            tuple(row.zones) or zones,
            hosts,
            joined,
            suggested,
            adoptable,
        )

    def page(
        self, query: PageQuery, link: LinkFilter | None = None, zone: str | None = None
    ) -> Page[DiscoveredView]:
        """One page of discovered host names (search, link filter, zone, sort)."""
        with self._uow:
            rows, total = self._uow.discovery.page(query, link, zone)
            views = [DiscoveredView(row, self._link(domain)) for row, domain in rows]
        return Page.of(views, total, query)

    def link_of(self, hostname: str) -> LinkView | None:
        """The Jendock domain that already uses ``hostname``, if any."""
        with self._uow:
            return self._link(self._uow.domains.get_by_hostname(hostname))

    def _link(self, domain: Domain | None) -> LinkView | None:
        if domain is None:
            return None
        environment = self._uow.environments.get(domain.environment_id)
        project = self._uow.projects.get(environment.project_id) if environment else None
        if environment is None or project is None:
            return None
        return LinkView(domain.id, project.slug, environment.name, domain.origin)
