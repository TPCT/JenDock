"""Integrations schemas. No response model has a field that could carry a credential."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.adapters.integrations_client import CloudflareTest
from backend.services.integrations import (
    CloudflareOverview,
    DeliveryView,
    DeployKeyView,
    GithubOverview,
    IngressValues,
    Reason,
)
from backend.services.platform_secrets import MAX_TOKEN_LENGTH
from backend.services.platform_settings import IngressOverrides, IngressSettingsPatch


class SetCloudflareTokenRequest(BaseModel):
    """Body of ``PUT /api/integrations/cloudflare/token``; the value is write-only."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(min_length=1, max_length=MAX_TOKEN_LENGTH, repr=False)


class IngressSettingsRequest(BaseModel):
    """Body of ``PUT /api/integrations/cloudflare/settings``.

    Only the fields that are present change; ``null`` (or an empty string) resets a value to the
    environment default.
    """

    model_config = ConfigDict(extra="forbid")

    acme_email: str | None = Field(default=None, max_length=254)
    public_ipv4: str | None = Field(default=None, max_length=15)
    public_ipv6: str | None = Field(default=None, max_length=45)
    acme_challenge: Literal["dns-01", "http-01"] | None = None

    def to_patch(self) -> IngressSettingsPatch:
        """The service-layer change."""
        values = IngressOverrides(
            acme_email=self.acme_email,
            public_ipv4=self.public_ipv4,
            public_ipv6=self.public_ipv6,
            acme_challenge=self.acme_challenge,
        )
        return IngressSettingsPatch(values, frozenset(self.model_fields_set))


class IngressValuesResponse(BaseModel):
    """Each editable value as stored, as defaulted by the environment and as in effect."""

    stored: IngressSettingsValues
    defaults: IngressSettingsValues
    effective: IngressSettingsValues

    @classmethod
    def from_values(cls, values: IngressValues) -> IngressValuesResponse:
        """Build from the service view."""
        return cls(
            stored=IngressSettingsValues.from_overrides(values.stored),
            defaults=IngressSettingsValues.from_overrides(values.defaults),
            effective=IngressSettingsValues.from_overrides(values.effective),
        )


class IngressSettingsValues(BaseModel):
    """The four editable ingress values."""

    acme_email: str | None
    public_ipv4: str | None
    public_ipv6: str | None
    acme_challenge: str | None

    @classmethod
    def from_overrides(cls, value: IngressOverrides) -> IngressSettingsValues:
        """Build from the service view."""
        return cls(
            acme_email=value.acme_email,
            public_ipv4=value.public_ipv4,
            public_ipv6=value.public_ipv6,
            acme_challenge=value.acme_challenge,
        )


IngressValuesResponse.model_rebuild()


class ReasonResponse(BaseModel):
    """Why ingress cannot work yet, in words an operator can act on."""

    code: str
    message: str
    blocking: bool

    @classmethod
    def from_reason(cls, reason: Reason) -> ReasonResponse:
        """Build from the service view."""
        return cls(code=reason.code, message=reason.message, blocking=reason.blocking)


class HelperResponse(BaseModel):
    """Whether the host helper's socket answers."""

    enabled: bool
    socket_path: str
    reachable: bool
    reason: str | None


class CloudflareResponse(BaseModel):
    """The Cloudflare section: token presence (never its value), settings and readiness."""

    token_configured: bool
    token_updated_at: datetime | None
    settings: IngressValuesResponse
    helper: HelperResponse | None
    ready: bool
    reasons: list[ReasonResponse]

    @classmethod
    def from_overview(cls, overview: CloudflareOverview) -> CloudflareResponse:
        """Build from the service view."""
        state = overview.ingress
        return cls(
            token_configured=overview.token.configured,
            token_updated_at=overview.token.updated_at,
            settings=IngressValuesResponse.from_values(overview.values),
            helper=(
                HelperResponse(
                    enabled=state.enabled,
                    socket_path=state.helper_socket_path,
                    reachable=state.helper_reachable,
                    reason=state.helper_reason,
                )
                if state
                else None
            ),
            ready=overview.ready,
            reasons=[ReasonResponse.from_reason(r) for r in overview.reasons],
        )


class IngressReadinessResponse(BaseModel):
    """Whether domains can be applied; used by the Domains page."""

    ready: bool
    token_configured: bool
    reasons: list[ReasonResponse]

    @classmethod
    def from_overview(cls, overview: CloudflareOverview) -> IngressReadinessResponse:
        """Build from the service view."""
        no_token = any(r.code == "no_token" for r in overview.reasons)
        return cls(
            ready=overview.ready,
            token_configured=not no_token,
            reasons=[ReasonResponse.from_reason(r) for r in overview.reasons],
        )


class CloudflareTestResponse(BaseModel):
    """Result of verifying the token: zone names only."""

    ok: bool
    status: str | None
    zones: list[str]
    message: str

    @classmethod
    def from_test(cls, result: CloudflareTest) -> CloudflareTestResponse:
        """Build from the worker's answer."""
        return cls(
            ok=result.ok, status=result.status, zones=list(result.zones), message=result.message
        )


class DeliveryResponse(BaseModel):
    """One recorded webhook delivery."""

    delivery_id: str
    event: str
    repository: str | None
    outcome: str
    received_at: datetime

    @classmethod
    def from_view(cls, view: DeliveryView) -> DeliveryResponse:
        """Build from the service view."""
        return cls(
            delivery_id=view.delivery_id,
            event=view.event,
            repository=view.repository,
            outcome=view.outcome,
            received_at=view.received_at,
        )


class DeployKeyResponse(BaseModel):
    """A project's deploy key: only its public half."""

    slug: str
    name: str
    repository: str
    git_transport: str
    exists: bool
    public_key: str | None

    @classmethod
    def from_view(cls, view: DeployKeyView) -> DeployKeyResponse:
        """Build from the service view."""
        return cls(
            slug=view.slug,
            name=view.name,
            repository=view.repository,
            git_transport=view.transport,
            exists=view.exists,
            public_key=view.public_key,
        )


class WebhookResponse(BaseModel):
    """The webhook as GitHub must be configured, and how deliveries went."""

    payload_url: str
    secret_configured: bool | None
    counts_7d: dict[str, int]
    recent: list[DeliveryResponse]


class GithubIntegrationResponse(BaseModel):
    """The GitHub page."""

    webhook: WebhookResponse
    deploy_keys: list[DeployKeyResponse]

    @classmethod
    def from_overview(cls, overview: GithubOverview) -> GithubIntegrationResponse:
        """Build from the service view."""
        return cls(
            webhook=WebhookResponse(
                payload_url=overview.payload_url,
                secret_configured=overview.secret_configured,
                counts_7d=overview.counts,
                recent=[DeliveryResponse.from_view(d) for d in overview.recent],
            ),
            deploy_keys=[DeployKeyResponse.from_view(k) for k in overview.keys],
        )


class DeployKeyCreatedResponse(BaseModel):
    """A newly created deploy key (public half)."""

    slug: str
    public_key: str


class McpToolResponse(BaseModel):
    """One tool of the MCP server."""

    name: str
    description: str
    scope: str
    read_only: bool


class McpToolsResponse(BaseModel):
    """The MCP endpoint and the tools a scope selection opens up."""

    endpoint: str
    enabled: bool
    tools: list[McpToolResponse]
