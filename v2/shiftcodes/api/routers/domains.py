"""Domain endpoints: definition, preview-then-apply, removal, status and certificates.

Reads need scope ``read``; every mutation needs ``admin`` and is audited by the service or the
worker. Changes to DNS, Apache and TLS happen in the worker, never in the API process.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Response

from shiftcodes.api.deps import Container, require_scope
from shiftcodes.api.principal import Principal
from shiftcodes.api.schemas.domains import (
    CertificateOverviewResponse,
    CertificateResponse,
    CheckResponse,
    CreateDomainRequest,
    DomainDetailResponse,
    DomainResponse,
    PlanRequest,
    PlanResponse,
    PreviewRequest,
    UpdateDomainRequest,
)
from shiftcodes.domain.enums import EnvironmentName, TokenScope

router = APIRouter(tags=["domains"])
Reader = Annotated[Principal, Depends(require_scope(TokenScope.READ))]
Admin = Annotated[Principal, Depends(require_scope(TokenScope.ADMIN))]
BASE = "/api/projects/{slug}/environments/{env}/domains"


@dataclass(frozen=True, slots=True)
class EnvPath:
    """Path parameters identifying an environment."""

    slug: str
    env: EnvironmentName


@dataclass(frozen=True, slots=True)
class DomainPath:
    """Path parameters identifying one domain of an environment."""

    slug: str
    env: EnvironmentName
    domain_id: int


@router.get(BASE)
def list_domains(
    path: Annotated[EnvPath, Depends()], _principal: Reader, container: Container
) -> list[DomainDetailResponse]:
    """The domains of an environment with certificate health and the latest check."""
    views = container.domain_status_service().list(path.slug, path.env)
    return [DomainDetailResponse.from_status(view) for view in views]


@router.post(BASE, status_code=201)
def create_domain(
    path: Annotated[EnvPath, Depends()],
    body: CreateDomainRequest,
    principal: Admin,
    container: Container,
) -> DomainResponse:
    """Define a domain (nothing is applied yet; use preview and apply)."""
    domain = container.domain_service().create(
        path.slug, path.env, body.to_draft(), principal.actor
    )
    return DomainResponse.from_domain(domain)


@router.get(BASE + "/{domain_id}")
def get_domain(
    path: Annotated[DomainPath, Depends()], _principal: Reader, container: Container
) -> DomainDetailResponse:
    """One domain with certificate health and the latest check."""
    view = container.domain_status_service().status(path.slug, path.env, path.domain_id)
    return DomainDetailResponse.from_status(view)


@router.patch(BASE + "/{domain_id}")
def update_domain(
    path: Annotated[DomainPath, Depends()],
    body: UpdateDomainRequest,
    principal: Admin,
    container: Container,
) -> DomainResponse:
    """Change the routing; preview and apply again to roll the change out."""
    domain = container.domain_service().update(
        path.slug, path.env, path.domain_id, body.to_patch(), principal.actor
    )
    return DomainResponse.from_domain(domain)


@router.delete(BASE + "/{domain_id}", status_code=204)
def delete_domain(
    path: Annotated[DomainPath, Depends()], principal: Admin, container: Container
) -> Response:
    """Delete a domain that is not applied (remove it first)."""
    container.domain_service().delete(path.slug, path.env, path.domain_id, principal.actor)
    return Response(status_code=204)


@router.post(BASE + "/{domain_id}/preview")
def preview_domain(
    path: Annotated[DomainPath, Depends()],
    body: PreviewRequest,
    principal: Admin,
    container: Container,
) -> PlanResponse:
    """Show the DNS, Apache vhost and TLS changes an apply (or remove) would make."""
    container.domain_service().get(path.slug, path.env, path.domain_id)
    plan = container.ingress.preview(path.domain_id, body.operation, principal.actor)
    return PlanResponse.from_view(plan)


@router.post(BASE + "/{domain_id}/apply")
def apply_domain(
    path: Annotated[DomainPath, Depends()],
    body: PlanRequest,
    principal: Admin,
    container: Container,
) -> PlanResponse:
    """Apply the previewed plan; 409 when the plan changed since the preview."""
    container.domain_service().get(path.slug, path.env, path.domain_id)
    plan = container.ingress.apply(path.domain_id, body.plan_hash, principal.actor)
    return PlanResponse.from_view(plan)


@router.post(BASE + "/{domain_id}/remove")
def remove_domain(
    path: Annotated[DomainPath, Depends()],
    body: PlanRequest,
    principal: Admin,
    container: Container,
) -> PlanResponse:
    """Remove the previewed domain from Apache, TLS and DNS (own records only)."""
    container.domain_service().get(path.slug, path.env, path.domain_id)
    plan = container.ingress.remove(path.domain_id, body.plan_hash, principal.actor)
    return PlanResponse.from_view(plan)


@router.post(BASE + "/{domain_id}/check")
def check_domain(
    path: Annotated[DomainPath, Depends()], principal: Admin, container: Container
) -> CheckResponse:
    """Run DNS, HTTPS and backend checks now and store the result with its timestamp."""
    container.domain_service().get(path.slug, path.env, path.domain_id)
    return CheckResponse.from_result(container.ingress.check(path.domain_id, principal.actor))


@router.post(BASE + "/{domain_id}/certificate")
def issue_certificate(
    path: Annotated[DomainPath, Depends()], principal: Admin, container: Container
) -> CertificateResponse:
    """Issue or renew the certificate now (renewal otherwise happens 30 days before expiry)."""
    container.domain_service().get(path.slug, path.env, path.domain_id)
    issued = container.ingress.issue_certificate(path.domain_id, principal.actor)
    return CertificateResponse(
        not_after=issued.not_after,
        days_remaining=issued.days_remaining,
        state=issued.state,
        last_attempt_at=None,
        last_error=None,
    )


@router.get("/api/ingress/certificates")
def list_certificates(
    _principal: Reader, container: Container
) -> list[CertificateOverviewResponse]:
    """Expiry monitoring across all domains, soonest expiry first."""
    items = container.domain_status_service().certificates()
    return [CertificateOverviewResponse.from_overview(item) for item in items]
