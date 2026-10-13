"""Domain discovery and adoption: what Cloudflare and Apache already serve, and linking it.

Reading the discovered domains and refreshing them is for owners and admins (it spans every
project). Adopting a discovered domain into a project is for a maintainer of that project (or an
admin); "adopt all suggested" spans projects and is admin only. Nothing here changes DNS or
Apache: adoption only records the domain in Jendock. Reading Cloudflare and Apache happens in
the worker.

The zone list and the name lookup of the "new domain" forms come in two forms: environment
scoped for maintainers, and global for the New Project wizard (the project does not exist yet).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from backend.api.deps import Container, Target, require
from backend.api.paging import Paging
from backend.api.principal import Principal
from backend.api.schemas.discovery import (
    AdoptDomainRequest,
    AdoptSuggestedRequest,
    AdoptSuggestedResponse,
    DiscoveredPage,
    DiscoveryStateResponse,
    LinkResponse,
    LookupResponse,
    ZonesResponse,
)
from backend.api.schemas.domains import DomainResponse
from backend.domain.access import Action
from backend.domain.discovery import LinkFilter
from backend.domain.enums import EnvironmentName
from backend.domain.hostnames import Hostname

router = APIRouter(tags=["domains"])
Reader = Annotated[Principal, Depends(require(Action.DOMAIN_DISCOVER, Target.NONE))]
Refresher = Annotated[Principal, Depends(require(Action.DOMAIN_DISCOVER_REFRESH, Target.NONE))]
BulkAdopter = Annotated[Principal, Depends(require(Action.DOMAIN_ADOPT_ALL, Target.NONE))]
Adopter = Annotated[Principal, Depends(require(Action.DOMAIN_ADOPT))]
Looker = Annotated[Principal, Depends(require(Action.DOMAIN_LOOKUP))]
GLOBAL_BASE = "/api/domains"
ENV_BASE = "/api/projects/{slug}/environments/{env}/domains"
Hostnames = Annotated[str, Query(min_length=1, max_length=253)]


def _lookup(container: Container, principal: Principal, hostname: str) -> LookupResponse:
    name = Hostname.parse(hostname, allow_wildcard=True).value
    found = container.discovery.lookup(name, principal.actor)
    link = container.discovery_queries().link_of(name)
    used = (
        LinkResponse(
            domain_id=link.domain_id,
            project=link.project,
            environment=link.environment,
            origin=link.origin,
        )
        if link
        else None
    )
    return LookupResponse.from_lookup(found, used)


# --- global (owners and admins) ---------------------------------------------------------------


@router.get(GLOBAL_BASE + "/discovery")
def get_discovery(_principal: Reader, container: Container) -> DiscoveryStateResponse:
    """What the last refresh could read (Cloudflare, the host helper) and the page's counts."""
    return DiscoveryStateResponse.from_view(container.discovery_queries().state())


@router.post(GLOBAL_BASE + "/discovery/refresh")
def refresh_discovery(principal: Refresher, container: Container) -> DiscoveryStateResponse:
    """Read Cloudflare and Apache again (read-only) and replace the discovered domains."""
    container.discovery.refresh(principal.actor)
    return DiscoveryStateResponse.from_view(container.discovery_queries().state())


@router.get(GLOBAL_BASE + "/discovered")
def list_discovered(
    _principal: Reader,
    container: Container,
    paging: Paging,
    link: Annotated[LinkFilter | None, Query()] = None,
    zone: Annotated[str | None, Query(max_length=253)] = None,
) -> DiscoveredPage:
    """Discovered host names: search (``q``), ``link`` (linked/unlinked/suggested), ``zone``."""
    page = container.discovery_queries().page(paging.query, link, zone)
    return DiscoveredPage.from_page(page)


@router.post(GLOBAL_BASE + "/adopt-suggested")
def adopt_suggested(
    body: AdoptSuggestedRequest, principal: BulkAdopter, container: Container
) -> AdoptSuggestedResponse:
    """Adopt every unlinked name whose suggestion is certain (Apache's port is a container's)."""
    result = container.adoption_service().adopt_suggested(body.hostnames, principal.actor)
    return AdoptSuggestedResponse.from_result(result)


@router.get(GLOBAL_BASE + "/zones")
def list_zones(principal: Reader, container: Container) -> ZonesResponse:
    """The Cloudflare zones the stored token can see (for the New Project wizard)."""
    return ZonesResponse.from_zones(container.discovery.zones(principal.actor))


@router.get(GLOBAL_BASE + "/lookup")
def lookup_name(principal: Reader, container: Container, hostname: Hostnames) -> LookupResponse:
    """Which DNS records already exist for ``hostname`` (the wizard's conflict warning)."""
    return _lookup(container, principal, hostname)


# --- one environment (maintainers) -------------------------------------------------------------
# Registered before the domain routes so that "zones" is never read as a domain id.


@router.get(ENV_BASE + "/zones")
def list_environment_zones(
    slug: str, env: EnvironmentName, principal: Looker, container: Container
) -> ZonesResponse:
    """The Cloudflare zones the stored token can see (for the "new domain" form)."""
    del slug, env  # the path only scopes the permission check
    return ZonesResponse.from_zones(container.discovery.zones(principal.actor))


@router.get(ENV_BASE + "/lookup")
def lookup_environment_name(
    slug: str, env: EnvironmentName, principal: Looker, container: Container, hostname: Hostnames
) -> LookupResponse:
    """Which DNS records already exist for ``hostname`` (the form's conflict warning)."""
    del slug, env
    return _lookup(container, principal, hostname)


@router.post(ENV_BASE + "/adopt", status_code=201)
def adopt_domain(
    slug: str,
    env: EnvironmentName,
    body: AdoptDomainRequest,
    principal: Adopter,
    container: Container,
) -> DomainResponse:
    """Record a discovered domain as linked to this environment (DNS and Apache stay as is)."""
    domain = container.adoption_service().adopt(slug, env, body.to_request(), principal.actor)
    return DomainResponse.from_domain(domain)
