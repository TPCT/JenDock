"""Notification endpoints: channels (secrets go through the worker), rules and history.

Everything needs the ``admin`` scope except the read-only policy. The API never sees a stored
secret: a channel's webhook URL or SMTP credentials are forwarded to the worker on write and
never returned.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from backend.api.deps import Container, Target, require
from backend.api.paging import Paging
from backend.api.principal import Principal
from backend.api.schemas.notifications import (
    ChannelResponse,
    CreateChannelRequest,
    DeliveryPageResponse,
    PolicyResponse,
    RulesRequest,
    RulesResponse,
    TestResultResponse,
    UpdateChannelRequest,
)
from backend.config_notifications import get_notification_settings
from backend.domain.access import Action
from backend.domain.enums import EnvironmentName
from backend.domain.errors import ValidationError
from backend.domain.notifications import DeliveryStatus
from backend.services.notification_rules import RuleScope

router = APIRouter(prefix="/api", tags=["notifications"])
Reader = Annotated[Principal, Depends(require(Action.NOTIFICATION_POLICY, Target.NONE))]
Admin = Annotated[Principal, Depends(require(Action.NOTIFICATION_MANAGE, Target.NONE))]


@dataclass(frozen=True, slots=True)
class ProjectPath:
    """Path parameters identifying a project."""

    slug: str


@dataclass(frozen=True, slots=True)
class EnvPath:
    """Path parameters identifying an environment."""

    slug: str
    env: EnvironmentName


@router.get("/notifications/policy")
def get_policy(_principal: Reader) -> PolicyResponse:
    """The event catalogue and the operator-set delivery policy."""
    return PolicyResponse.from_settings(get_notification_settings())


# --- channels ------------------------------------------------------------------------------


@router.get("/notifications/channels")
def list_channels(_principal: Admin, container: Container) -> list[ChannelResponse]:
    """All channels; secrets are never included."""
    channels = container.notification_queries().channels()
    return [ChannelResponse.from_model(channel) for channel in channels]


@router.post("/notifications/channels", status_code=201)
def create_channel(
    body: CreateChannelRequest, principal: Admin, container: Container
) -> ChannelResponse:
    """Create a channel; the worker validates and encrypts its configuration."""
    channel_id = container.notification_gateway().create_channel(
        body.name, body.kind, body.config, principal.actor
    )
    return ChannelResponse.from_model(container.notification_queries().channel(channel_id))


@router.patch("/notifications/channels/{channel_id}")
def update_channel(
    channel_id: int, body: UpdateChannelRequest, principal: Admin, container: Container
) -> ChannelResponse:
    """Change a channel; fields left out of ``config`` keep their stored value."""
    container.notification_gateway().update_channel(
        channel_id,
        principal.actor,
        name=body.name,
        enabled=body.enabled,
        config=body.config,
    )
    return ChannelResponse.from_model(container.notification_queries().channel(channel_id))


@router.delete("/notifications/channels/{channel_id}", status_code=204)
def delete_channel(channel_id: int, principal: Admin, container: Container) -> Response:
    """Delete a channel together with its rules and delivery history."""
    container.notification_gateway().delete_channel(channel_id, principal.actor)
    return Response(status_code=204)


@router.post("/notifications/channels/{channel_id}/test")
def test_channel(channel_id: int, principal: Admin, container: Container) -> TestResultResponse:
    """Send a test message now. A failed delivery is a 200 with ``ok: false``."""
    ok, error = container.notification_gateway().test_channel(channel_id, principal.actor)
    return TestResultResponse(ok=ok, error=error)


# --- rules ---------------------------------------------------------------------------------


@router.get("/notifications/rules")
def get_default_rules(_principal: Admin, container: Container) -> RulesResponse:
    """The global default rules."""
    return RulesResponse.from_view(container.notification_rules().get(RuleScope()))


@router.put("/notifications/rules")
def put_default_rules(body: RulesRequest, principal: Admin, container: Container) -> RulesResponse:
    """Replace the global default rules."""
    view = container.notification_rules().replace(RuleScope(), body.inputs(), principal.actor)
    return RulesResponse.from_view(view)


@router.get("/projects/{slug}/notifications/rules")
def get_project_rules(
    path: Annotated[ProjectPath, Depends()], _principal: Admin, container: Container
) -> RulesResponse:
    """The project's own rules (empty means the default rules apply)."""
    return RulesResponse.from_view(container.notification_rules().get(RuleScope(path.slug)))


@router.put("/projects/{slug}/notifications/rules")
def put_project_rules(
    path: Annotated[ProjectPath, Depends()],
    body: RulesRequest,
    principal: Admin,
    container: Container,
) -> RulesResponse:
    """Replace the project's rules."""
    scope = RuleScope(path.slug)
    view = container.notification_rules().replace(scope, body.inputs(), principal.actor)
    return RulesResponse.from_view(view)


@router.get("/projects/{slug}/environments/{env}/notifications/rules")
def get_environment_rules(
    path: Annotated[EnvPath, Depends()], _principal: Admin, container: Container
) -> RulesResponse:
    """One environment's own rules (empty means the project's or the default rules apply)."""
    scope = RuleScope(path.slug, path.env)
    return RulesResponse.from_view(container.notification_rules().get(scope))


@router.put("/projects/{slug}/environments/{env}/notifications/rules")
def put_environment_rules(
    path: Annotated[EnvPath, Depends()],
    body: RulesRequest,
    principal: Admin,
    container: Container,
) -> RulesResponse:
    """Replace one environment's rules."""
    scope = RuleScope(path.slug, path.env)
    view = container.notification_rules().replace(scope, body.inputs(), principal.actor)
    return RulesResponse.from_view(view)


# --- history -------------------------------------------------------------------------------


@router.get("/notifications/deliveries")
def list_deliveries(
    _principal: Admin,
    container: Container,
    paging: Paging,
    project: Annotated[str | None, Query(max_length=64)] = None,
    channel_id: Annotated[int | None, Query(ge=1)] = None,
    status: Annotated[str | None, Query(max_length=16)] = None,
) -> DeliveryPageResponse:
    """The delivery history, newest first; ``project`` limits it to one project.

    ``q`` matches title, event and last error; ``sort`` is ``created`` or ``status``.
    """
    if status is not None and status not in {s.value for s in DeliveryStatus}:
        raise ValidationError("unknown status")
    page = container.notification_queries().deliveries(
        paging.query, slug=project, channel_id=channel_id, status=status
    )
    return DeliveryPageResponse.from_page(page)
