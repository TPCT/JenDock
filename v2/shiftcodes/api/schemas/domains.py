"""Domain request and response schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from shiftcodes.domain.ingress import (
    CertificateState,
    DnsPlanView,
    DnsRecordType,
    DnsRecordView,
    DomainStatus,
    IngressPlanView,
    PlanAction,
    TlsPlanView,
    VhostPlanView,
)
from shiftcodes.repositories.models import Domain
from shiftcodes.services.certificates import CertificateStatus
from shiftcodes.services.domain_checks import CheckResult
from shiftcodes.services.domain_status import DomainStatus as DomainStatusView
from shiftcodes.services.domains import DomainDraft, DomainPatch

MAX_HOSTNAME = 253
MAX_SERVICE = 63
MAX_TARGET = 255
PLAN_HASH_LENGTH = 64


class CreateDomainRequest(BaseModel):
    """Body of ``POST .../domains``."""

    model_config = ConfigDict(extra="forbid")

    hostname: str = Field(min_length=1, max_length=MAX_HOSTNAME)
    service: str = Field(min_length=1, max_length=MAX_SERVICE)
    container_port: int = Field(ge=1, le=65535)
    published_port: int = Field(ge=1024, le=65535)
    dns_record_type: DnsRecordType = DnsRecordType.A
    dns_target: str | None = Field(default=None, max_length=MAX_TARGET)
    dns_proxied: bool = False
    tls_enabled: bool = True

    def to_draft(self) -> DomainDraft:
        """The service-layer definition."""
        return DomainDraft(
            hostname=self.hostname,
            service=self.service,
            container_port=self.container_port,
            published_port=self.published_port,
            dns_record_type=self.dns_record_type,
            dns_target=self.dns_target,
            dns_proxied=self.dns_proxied,
            tls_enabled=self.tls_enabled,
        )


class UpdateDomainRequest(BaseModel):
    """Body of ``PATCH .../domains/{id}``; absent fields stay, ``dns_target: null`` resets."""

    model_config = ConfigDict(extra="forbid")

    service: str | None = Field(default=None, min_length=1, max_length=MAX_SERVICE)
    container_port: int | None = Field(default=None, ge=1, le=65535)
    published_port: int | None = Field(default=None, ge=1024, le=65535)
    dns_record_type: DnsRecordType | None = None
    dns_target: str | None = Field(default=None, max_length=MAX_TARGET)
    dns_proxied: bool | None = None
    tls_enabled: bool | None = None

    def to_patch(self) -> DomainPatch:
        """The service-layer patch."""
        return DomainPatch(
            service=self.service,
            container_port=self.container_port,
            published_port=self.published_port,
            dns_record_type=self.dns_record_type,
            dns_target=self.dns_target,
            clear_dns_target="dns_target" in self.model_fields_set and self.dns_target is None,
            dns_proxied=self.dns_proxied,
            tls_enabled=self.tls_enabled,
        )


class PreviewRequest(BaseModel):
    """Body of ``POST .../domains/{id}/preview``."""

    model_config = ConfigDict(extra="forbid")

    operation: Literal["apply", "remove"] = "apply"


class PlanRequest(BaseModel):
    """Body of apply and remove: the hash of the previewed plan."""

    model_config = ConfigDict(extra="forbid")

    plan_hash: str = Field(min_length=PLAN_HASH_LENGTH, max_length=PLAN_HASH_LENGTH)


class DomainResponse(BaseModel):
    """A domain."""

    id: int
    hostname: str
    service: str
    container_port: int
    published_port: int
    dns_record_type: DnsRecordType
    dns_target: str | None
    dns_proxied: bool
    tls_enabled: bool
    status: DomainStatus
    status_message: str | None
    applied_at: datetime | None
    last_verified_at: datetime | None
    last_verified_ok: bool | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, domain: Domain) -> DomainResponse:
        """Build the response from the stored row."""
        return cls(
            id=domain.id,
            hostname=domain.hostname,
            service=domain.service,
            container_port=domain.container_port,
            published_port=domain.published_port,
            dns_record_type=domain.dns_record_type,
            dns_target=domain.dns_target,
            dns_proxied=domain.dns_proxied,
            tls_enabled=domain.tls_enabled,
            status=domain.status,
            status_message=domain.status_message,
            applied_at=domain.applied_at,
            last_verified_at=domain.last_verified_at,
            last_verified_ok=domain.last_verified_ok,
            created_at=domain.created_at,
            updated_at=domain.updated_at,
        )


class CertificateResponse(BaseModel):
    """Certificate expiry health."""

    not_after: datetime
    days_remaining: int
    state: CertificateState
    last_attempt_at: datetime | None
    last_error: str | None

    @classmethod
    def from_status(cls, status: CertificateStatus) -> CertificateResponse:
        """Build the response from the service view."""
        return cls(
            not_after=status.not_after,
            days_remaining=status.days_remaining,
            state=status.state,
            last_attempt_at=status.last_attempt_at,
            last_error=status.last_error,
        )


class CertificateOverviewResponse(CertificateResponse):
    """One row of the expiry monitoring list."""

    domain_id: int
    hostname: str

    @classmethod
    def from_overview(cls, status: CertificateStatus) -> CertificateOverviewResponse:
        """Build the row from the service view."""
        return cls(
            domain_id=status.domain_id,
            hostname=status.hostname,
            not_after=status.not_after,
            days_remaining=status.days_remaining,
            state=status.state,
            last_attempt_at=status.last_attempt_at,
            last_error=status.last_error,
        )


class CheckResponse(BaseModel):
    """The latest status check: ``results`` has ``dns``, ``https`` and ``backend`` parts."""

    checked_at: datetime
    ok: bool
    results: dict[str, object]

    @classmethod
    def from_result(cls, result: CheckResult) -> CheckResponse:
        """Build the response from the service view."""
        return cls(checked_at=result.checked_at, ok=result.ok, results=dict(result.results))


class DomainDetailResponse(DomainResponse):
    """A domain with certificate health and the latest check."""

    certificate: CertificateResponse | None
    check: CheckResponse | None

    @classmethod
    def from_status(cls, view: DomainStatusView) -> DomainDetailResponse:
        """Build the response from the status view."""
        base = DomainResponse.from_domain(view.domain)
        return cls(
            **base.model_dump(),
            certificate=CertificateResponse.from_status(view.certificate)
            if view.certificate
            else None,
            check=CheckResponse.from_result(view.check) if view.check else None,
        )


class DnsRecordResponse(BaseModel):
    """A DNS record in a preview."""

    record_type: str
    name: str
    content: str
    proxied: bool

    @classmethod
    def from_view(cls, record: DnsRecordView | None) -> DnsRecordResponse | None:
        """Build from the plan view."""
        if record is None:
            return None
        return cls(
            record_type=record.record_type,
            name=record.name,
            content=record.content,
            proxied=record.proxied,
        )


class DnsPlanResponse(BaseModel):
    """DNS part of a plan."""

    action: PlanAction
    zone: str
    current: DnsRecordResponse | None
    desired: DnsRecordResponse | None
    removes: list[DnsRecordResponse]
    message: str

    @classmethod
    def from_view(cls, plan: DnsPlanView) -> DnsPlanResponse:
        """Build from the plan view."""
        return cls(
            action=plan.action,
            zone=plan.zone,
            current=DnsRecordResponse.from_view(plan.current),
            desired=DnsRecordResponse.from_view(plan.desired),
            removes=[r for item in plan.removes if (r := DnsRecordResponse.from_view(item))],
            message=plan.message,
        )


class VhostPlanResponse(BaseModel):
    """Apache part of a plan with the unified diff."""

    action: PlanAction
    current: str | None
    proposed: str | None
    diff: str

    @classmethod
    def from_view(cls, plan: VhostPlanView) -> VhostPlanResponse:
        """Build from the plan view."""
        return cls(action=plan.action, current=plan.current, proposed=plan.proposed, diff=plan.diff)


class TlsPlanResponse(BaseModel):
    """Certificate part of a plan."""

    action: str
    challenge: str | None
    not_after: datetime | None

    @classmethod
    def from_view(cls, plan: TlsPlanView) -> TlsPlanResponse:
        """Build from the plan view."""
        return cls(action=plan.action, challenge=plan.challenge, not_after=plan.not_after)


class PlanResponse(BaseModel):
    """A preview (or the applied plan): send ``plan_hash`` back to apply it."""

    domain_id: int
    hostname: str
    operation: str
    blocked: bool
    dns: DnsPlanResponse
    vhost: VhostPlanResponse
    tls: TlsPlanResponse
    steps: list[str]
    plan_hash: str

    @classmethod
    def from_view(cls, plan: IngressPlanView) -> PlanResponse:
        """Build from the plan view."""
        return cls(
            domain_id=plan.domain_id,
            hostname=plan.hostname,
            operation=plan.operation,
            blocked=plan.blocked,
            dns=DnsPlanResponse.from_view(plan.dns),
            vhost=VhostPlanResponse.from_view(plan.vhost),
            tls=TlsPlanResponse.from_view(plan.tls),
            steps=list(plan.steps),
            plan_hash=plan.plan_hash,
        )
