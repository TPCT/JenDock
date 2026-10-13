"""Notification schemas. Channel secrets are write-only: no response carries a URL or a
credential, only ``configured``, the last 4 characters of a webhook URL and non-secret facts."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from backend.api.paging import LegacyPageResponse
from backend.config_notifications import NotificationSettings
from backend.domain.notifications import CATALOGUE, ChannelKind, EventInfo
from backend.domain.paging import Page
from backend.repositories.notification_models import NotificationChannel
from backend.services.notification_rules import DeliveryView, RuleInput, RulesView

ConfigValue = str | int | bool | list[str] | None


class CreateChannelRequest(BaseModel):
    """Body of ``POST /api/notifications/channels``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=80)
    kind: ChannelKind
    config: dict[str, ConfigValue] = Field(repr=False)


class UpdateChannelRequest(BaseModel):
    """Body of ``PATCH /api/notifications/channels/{id}``; omitted fields stay unchanged."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=80)
    enabled: bool | None = None
    config: dict[str, ConfigValue] | None = Field(default=None, repr=False)


class ChannelResponse(BaseModel):
    """A channel without its secrets."""

    id: int
    name: str
    kind: ChannelKind
    enabled: bool
    configured: bool
    hint: str | None
    summary: dict[str, object]
    last_test_at: datetime | None
    last_test_ok: bool | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, channel: NotificationChannel) -> ChannelResponse:
        """Build the response; the ciphertext column is never touched."""
        return cls(
            id=channel.id,
            name=channel.name,
            kind=ChannelKind(channel.kind),
            enabled=channel.enabled,
            configured=True,
            hint=channel.hint,
            summary=dict(channel.summary),
            last_test_at=channel.last_test_at,
            last_test_ok=channel.last_test_ok,
            created_at=channel.created_at,
            updated_at=channel.updated_at,
        )


class TestResultResponse(BaseModel):
    """Outcome of "Send test"; ``error`` is a safe, short reason."""

    __test__ = False

    ok: bool
    error: str | None


class EventResponse(BaseModel):
    """One event of the catalogue."""

    event: str
    label: str
    severity: str
    project_scoped: bool

    @classmethod
    def from_info(cls, info: EventInfo) -> EventResponse:
        """Build from a catalogue entry."""
        return cls(
            event=info.event.value,
            label=info.label,
            severity=info.severity.value,
            project_scoped=info.project_scoped,
        )


class PolicyResponse(BaseModel):
    """The delivery policy (set by the operator through ``JENDOCK_NOTIFY_*``)."""

    enabled: bool
    dedupe_minutes: int
    max_attempts: int
    disk_threshold_percent: int
    webhook_failure_threshold: int
    webhook_failure_window_minutes: int
    events: list[EventResponse]

    @classmethod
    def from_settings(cls, settings: NotificationSettings) -> PolicyResponse:
        """Build from the settings plus the catalogue."""
        return cls(
            enabled=settings.enabled,
            dedupe_minutes=settings.dedupe_minutes,
            max_attempts=settings.max_attempts,
            disk_threshold_percent=settings.disk_threshold_percent,
            webhook_failure_threshold=settings.webhook_failure_threshold,
            webhook_failure_window_minutes=settings.webhook_failure_window_minutes,
            events=[EventResponse.from_info(info) for info in CATALOGUE],
        )


class RuleBody(BaseModel):
    """One channel's subscription."""

    model_config = ConfigDict(extra="forbid")

    channel_id: int
    events: list[str] = Field(max_length=20)
    enabled: bool = True


class RulesRequest(BaseModel):
    """Body of the ``PUT .../notifications/rules`` endpoints: the complete rule set."""

    model_config = ConfigDict(extra="forbid")

    rules: list[RuleBody] = Field(max_length=50)

    def inputs(self) -> list[RuleInput]:
        """The service form."""
        return [RuleInput(r.channel_id, list(r.events), r.enabled) for r in self.rules]


class RulesResponse(BaseModel):
    """The rules of a scope; ``inherited_from`` names the wider scope used when it has none."""

    scope: str
    rules: list[RuleBody]
    inherited_from: str | None

    @classmethod
    def from_view(cls, view: RulesView) -> RulesResponse:
        """Build from the service view."""
        return cls(
            scope=view.scope,
            rules=[
                RuleBody(channel_id=r.channel_id, events=r.events, enabled=r.enabled)
                for r in view.rules
            ],
            inherited_from=view.inherited_from,
        )


class DeliveryResponse(BaseModel):
    """One row of the delivery history. ``last_error`` never contains a URL or credential."""

    id: int
    channel_id: int
    channel_name: str
    event: str
    title: str
    project: str | None
    status: str
    attempts: int
    suppressed: int
    is_test: bool
    last_error: str | None
    created_at: datetime
    next_attempt_at: datetime | None
    delivered_at: datetime | None

    @classmethod
    def from_view(cls, view: DeliveryView) -> DeliveryResponse:
        """Build from a history row."""
        row = view.row
        return cls(
            id=row.id,
            channel_id=row.channel_id,
            channel_name=view.channel_name,
            event=row.event,
            title=row.title,
            project=view.project,
            status=row.status,
            attempts=row.attempts,
            suppressed=row.suppressed,
            is_test=row.is_test,
            last_error=row.last_error,
            created_at=row.created_at,
            next_attempt_at=row.next_attempt_at if row.status == "pending" else None,
            delivered_at=row.delivered_at,
        )


class DeliveryPageResponse(LegacyPageResponse[DeliveryResponse]):
    """A page of the delivery history."""

    @classmethod
    def from_page(cls, page: Page[DeliveryView]) -> DeliveryPageResponse:
        """Build from a page of history rows."""
        return cls.of(page, [DeliveryResponse.from_view(view) for view in page.items])
