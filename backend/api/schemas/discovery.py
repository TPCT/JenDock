"""Schemas of domain discovery and adoption."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.api.paging import PageResponse
from backend.api.schemas.domains import DomainResponse
from backend.domain.discovery import Confidence, IpSource, SourceState
from backend.domain.enums import EnvironmentName
from backend.domain.ingress import DomainOrigin
from backend.domain.paging import Page
from backend.services.discovery import NameLookup, RecordView, ZoneList
from backend.services.discovery_queries import DiscoveredView, DiscoveryStateView
from backend.services.domain_adoption import AdoptRequest, BulkResult

MAX_HOSTNAME = 253
MAX_SERVICE = 63
MAX_BULK = 500


class AdoptDomainRequest(BaseModel):
    """Body of ``POST .../domains/adopt``: which discovered name, answered by which container."""

    model_config = ConfigDict(extra="forbid")

    hostname: str = Field(min_length=1, max_length=MAX_HOSTNAME)
    service: str = Field(min_length=1, max_length=MAX_SERVICE)
    container_port: int = Field(ge=1, le=65535)
    published_port: int = Field(ge=1, le=65535)

    def to_request(self) -> AdoptRequest:
        """The service-layer request."""
        return AdoptRequest(self.hostname, self.service, self.container_port, self.published_port)


class AdoptSuggestedRequest(BaseModel):
    """Body of ``POST /api/domains/adopt-suggested``; no list means every certain suggestion."""

    model_config = ConfigDict(extra="forbid")

    hostnames: list[str] | None = Field(default=None, max_length=MAX_BULK)


class DnsRecordFound(BaseModel):
    """The Cloudflare record a discovered name has."""

    record_type: str
    content: str
    proxied: bool
    zone: str | None


class VhostFound(BaseModel):
    """The Apache site a discovered name has (only what the helper reports)."""

    file: str | None
    upstream_host: str | None
    upstream_port: int | None
    ssl: bool | None
    managed: bool


class SuggestionResponse(BaseModel):
    """Where the name probably belongs, and why."""

    project: str
    environment: EnvironmentName
    service: str | None
    container_port: int | None
    published_port: int | None
    confidence: Confidence
    reason: str


class LinkResponse(BaseModel):
    """The Jendock domain a discovered name is linked to."""

    domain_id: int
    project: str
    environment: EnvironmentName
    origin: DomainOrigin


class DiscoveredDomainResponse(BaseModel):
    """One discovered host name."""

    hostname: str
    zone: str | None
    status: Literal["linked", "suggested", "unlinked"]
    dns: DnsRecordFound | None
    points_to_server: bool | None
    vhost: VhostFound | None
    suggestion: SuggestionResponse | None
    link: LinkResponse | None
    seen_at: datetime

    @classmethod
    def from_view(cls, view: DiscoveredView) -> DiscoveredDomainResponse:
        """Build from the read model."""
        row = view.row
        dns = (
            DnsRecordFound(
                record_type=row.record_type,
                content=row.record_content or "",
                proxied=row.proxied,
                zone=row.zone_name,
            )
            if row.record_type
            else None
        )
        has_site = row.vhost_file is not None or row.upstream_port is not None
        vhost = (
            VhostFound(
                file=row.vhost_file,
                upstream_host=row.upstream_host,
                upstream_port=row.upstream_port,
                ssl=row.vhost_ssl,
                managed=row.vhost_managed,
            )
            if has_site
            else None
        )
        suggestion = (
            SuggestionResponse(
                project=row.suggested_project,
                environment=row.suggested_environment,
                service=row.suggested_service,
                container_port=row.suggested_container_port,
                published_port=row.suggested_published_port,
                confidence=row.suggestion_confidence,
                reason=row.suggestion_reason or "",
            )
            if row.suggested_project and row.suggested_environment and row.suggestion_confidence
            else None
        )
        link = (
            LinkResponse(
                domain_id=view.link.domain_id,
                project=view.link.project,
                environment=view.link.environment,
                origin=view.link.origin,
            )
            if view.link
            else None
        )
        status: Literal["linked", "suggested", "unlinked"] = (
            "linked" if link else "suggested" if suggestion else "unlinked"
        )
        return cls(
            hostname=row.hostname,
            zone=row.zone_name,
            status=status,
            dns=dns,
            points_to_server=row.points_to_server,
            vhost=vhost,
            suggestion=suggestion,
            link=link,
            seen_at=row.seen_at,
        )


class DiscoveredPage(PageResponse[DiscoveredDomainResponse]):
    """A page of discovered host names."""

    @classmethod
    def from_page(cls, page: Page[DiscoveredView]) -> DiscoveredPage:
        """Build from the read model page."""
        return cls.of(page, [DiscoveredDomainResponse.from_view(v) for v in page.items])


class SourceResponse(BaseModel):
    """Whether one source (Cloudflare, the host helper) could be read."""

    state: SourceState
    message: str | None


class DiscoveryStateResponse(BaseModel):
    """What the last refresh read, and what the page's filters and buttons need."""

    refreshed_at: datetime | None
    cloudflare: SourceResponse
    helper: SourceResponse
    server_ip: str | None
    ip_source: IpSource
    zones: list[str]
    hosts: int
    linked: int
    suggested: int
    adoptable: int

    @classmethod
    def from_view(cls, view: DiscoveryStateView) -> DiscoveryStateResponse:
        """Build from the read model."""
        return cls(
            refreshed_at=view.refreshed_at,
            cloudflare=SourceResponse(state=view.cloudflare, message=view.cloudflare_message),
            helper=SourceResponse(state=view.helper, message=view.helper_message),
            server_ip=view.server_ip,
            ip_source=view.ip_source,
            zones=list(view.zones),
            hosts=view.hosts,
            linked=view.linked,
            suggested=view.suggested,
            adoptable=view.adoptable,
        )


class SkippedResponse(BaseModel):
    """A name the bulk adoption left alone."""

    hostname: str
    reason: str


class AdoptSuggestedResponse(BaseModel):
    """Result of "adopt all suggested"."""

    adopted: list[DomainResponse]
    skipped: list[SkippedResponse]

    @classmethod
    def from_result(cls, result: BulkResult) -> AdoptSuggestedResponse:
        """Build from the service result."""
        return cls(
            adopted=[DomainResponse.from_domain(d) for d in result.adopted],
            skipped=[SkippedResponse(hostname=s.hostname, reason=s.reason) for s in result.skipped],
        )


class ZonesResponse(BaseModel):
    """The zones the stored Cloudflare token can see."""

    configured: bool
    zones: list[str]
    message: str

    @classmethod
    def from_zones(cls, result: ZoneList) -> ZonesResponse:
        """Build from the worker result."""
        return cls(configured=result.configured, zones=list(result.zones), message=result.message)


class ExistingRecord(BaseModel):
    """A DNS record that already exists for a looked-up name."""

    record_type: str
    content: str
    proxied: bool
    managed_by_jendock: bool

    @classmethod
    def from_view(cls, record: RecordView) -> ExistingRecord:
        """Build from the worker result."""
        return cls(
            record_type=record.record_type,
            content=record.content,
            proxied=record.proxied,
            managed_by_jendock=record.managed,
        )


class LookupResponse(BaseModel):
    """What exists for a host name: the conflict warning of the "new domain" forms."""

    hostname: str
    configured: bool
    zone: str | None
    records: list[ExistingRecord]
    in_use_by: LinkResponse | None
    message: str

    @classmethod
    def from_lookup(cls, result: NameLookup, in_use_by: LinkResponse | None) -> LookupResponse:
        """Build from the worker result and the platform's own domain table."""
        return cls(
            hostname=result.hostname,
            configured=result.configured,
            zone=result.zone,
            records=[ExistingRecord.from_view(r) for r in result.records],
            in_use_by=in_use_by,
            message=result.message,
        )
