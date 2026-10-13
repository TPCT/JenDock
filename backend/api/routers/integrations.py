"""Settings -> Integrations: Cloudflare, ingress readiness, GitHub webhook and deploy keys.

Everything that changes state is for browser sessions (an administrator signed in; CSRF applies)
and is audited. Credentials are write-only: the Cloudflare token goes to the worker and never
comes back, and only the public half of a deploy key is ever returned.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from backend.api.deps import Container, require_scope, require_session
from backend.api.principal import Principal
from backend.api.schemas.integrations import (
    CloudflareResponse,
    CloudflareTestResponse,
    DeployKeyCreatedResponse,
    GithubIntegrationResponse,
    IngressReadinessResponse,
    IngressSettingsRequest,
    IngressValuesResponse,
    SetCloudflareTokenRequest,
)
from backend.domain.enums import EnvironmentName, TokenScope

router = APIRouter(tags=["integrations"])
SessionPrincipal = Annotated[Principal, Depends(require_session())]
Reader = Annotated[Principal, Depends(require_scope(TokenScope.READ))]
BASE = "/api/integrations"


@router.get(BASE + "/cloudflare")
def get_cloudflare(_principal: SessionPrincipal, container: Container) -> CloudflareResponse:
    """Token presence, stored settings and why ingress cannot work yet (if it cannot)."""
    return CloudflareResponse.from_overview(container.integrations_service().cloudflare())


@router.put(BASE + "/cloudflare/token")
def put_cloudflare_token(
    body: SetCloudflareTokenRequest, principal: SessionPrincipal, container: Container
) -> CloudflareResponse:
    """Store the platform Cloudflare token (write-only; encrypted by the worker)."""
    service = container.integrations_service()
    service.set_token(body.token, principal.actor)
    return CloudflareResponse.from_overview(service.cloudflare())


@router.delete(BASE + "/cloudflare/token")
def delete_cloudflare_token(
    principal: SessionPrincipal, container: Container
) -> CloudflareResponse:
    """Forget the stored token."""
    service = container.integrations_service()
    service.clear_token(principal.actor)
    return CloudflareResponse.from_overview(service.cloudflare())


@router.post(BASE + "/cloudflare/test")
def test_cloudflare(principal: SessionPrincipal, container: Container) -> CloudflareTestResponse:
    """Verify the token with Cloudflare and list the zone names it can see."""
    result = container.integrations_service().test(principal.actor)
    return CloudflareTestResponse.from_test(result)


@router.put(BASE + "/cloudflare/settings")
def put_ingress_settings(
    body: IngressSettingsRequest, principal: SessionPrincipal, container: Container
) -> IngressValuesResponse:
    """Change the ACME email, public addresses or challenge; ``null`` restores the default."""
    values = container.integrations_service().update_settings(body.to_patch(), principal.actor)
    return IngressValuesResponse.from_values(values)


@router.get(BASE + "/github")
def get_github(principal: SessionPrincipal, container: Container) -> GithubIntegrationResponse:
    """Webhook URL and delivery summary, and every project's deploy key (public half)."""
    overview = container.integrations_service().github(
        container.settings.public_origin, principal.actor
    )
    return GithubIntegrationResponse.from_overview(overview)


@router.post(BASE + "/github/projects/{slug}/deploy-key", status_code=201)
def create_deploy_key(
    slug: str, principal: SessionPrincipal, container: Container
) -> DeployKeyCreatedResponse:
    """Create a project's deploy key in the worker (409 when it exists); public key returned."""
    public = container.integrations_service().create_deploy_key(slug, principal.actor)
    return DeployKeyCreatedResponse(slug=slug, public_key=public)


@router.get("/api/ingress/readiness")
def ingress_readiness(
    _principal: Reader,
    container: Container,
    project: Annotated[str | None, Query(max_length=64)] = None,
    environment: Annotated[EnvironmentName | None, Query()] = None,
) -> IngressReadinessResponse:
    """Whether domains can be applied; with a project and environment its own token counts."""
    overview = container.integrations_service().readiness(project, environment)
    return IngressReadinessResponse.from_overview(overview)
