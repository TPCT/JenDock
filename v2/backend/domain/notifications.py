"""Notification vocabulary: events, channels, their validation and the event payload.

Everything here is pure. Channel configuration (webhook URLs, SMTP credentials) is validated
here but only ever stored encrypted by the worker; ``ChannelConfig.hint`` and
``ChannelConfig.summary`` are the only parts the API may show.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlsplit

from backend.domain.errors import ValidationError

MAX_NAME = 80
MAX_URL = 1000
MAX_SECRET = 256
MAX_RECIPIENTS = 10
MAX_EMAIL = 254
MAX_TITLE = 200
MAX_TEXT = 500
MAX_LINE = 240
LOG_TAIL_LINES = 20
SHORT_SHA = 7
HINT_CHARS = 4
_LOCAL = r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}"
_DOMAIN = r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?"
_EMAIL = re.compile(rf"^{_LOCAL}@{_DOMAIN}$")
_HOST = re.compile(rf"^{_DOMAIN}$")


class NotificationEvent(StrEnum):
    """What can be announced. Values are stable identifiers used in rules and payloads."""

    RUN_FAILED = "run.failed"
    RUN_FIXED = "run.fixed"
    RUN_INTERRUPTED = "run.interrupted"
    DEPLOYMENT_SUCCEEDED = "deployment.succeeded"
    DEPLOYMENT_ROLLED_BACK = "deployment.rolled_back"
    DEPLOYMENT_ROLLBACK_FAILED = "deployment.rollback_failed"
    CERTIFICATE_EXPIRING = "certificate.expiring"
    CERTIFICATE_RENEWAL_FAILED = "certificate.renewal_failed"
    WEBHOOK_SIGNATURE_SPIKE = "webhook.signature_spike"
    DISK_USAGE_HIGH = "disk.usage_high"


class Severity(StrEnum):
    """How loud an event is."""

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass(frozen=True, slots=True)
class EventInfo:
    """Catalogue entry of one event."""

    event: NotificationEvent
    label: str
    severity: Severity
    project_scoped: bool  # False: only the global default rule can subscribe to it
    quiet_minutes: int  # minimum quiet period of this event (0 = the configured default)


_E = NotificationEvent
CATALOGUE: tuple[EventInfo, ...] = (
    EventInfo(_E.RUN_FAILED, "Run failed", Severity.CRITICAL, True, 0),
    EventInfo(_E.RUN_FIXED, "Run succeeded after a failure", Severity.INFO, True, 0),
    EventInfo(_E.RUN_INTERRUPTED, "Run interrupted", Severity.WARNING, True, 0),
    EventInfo(_E.DEPLOYMENT_SUCCEEDED, "Deployment succeeded", Severity.INFO, True, 0),
    EventInfo(_E.DEPLOYMENT_ROLLED_BACK, "Deployment rolled back", Severity.CRITICAL, True, 0),
    EventInfo(_E.DEPLOYMENT_ROLLBACK_FAILED, "Rollback failed", Severity.CRITICAL, True, 0),
    EventInfo(
        _E.CERTIFICATE_EXPIRING, "Certificate expires within 14 days", Severity.WARNING, True, 1440
    ),
    EventInfo(
        _E.CERTIFICATE_RENEWAL_FAILED, "Certificate renewal failed", Severity.CRITICAL, True, 360
    ),
    EventInfo(
        _E.WEBHOOK_SIGNATURE_SPIKE,
        "Webhook signature failures spike",
        Severity.WARNING,
        False,
        0,
    ),
    EventInfo(_E.DISK_USAGE_HIGH, "Disk usage above the threshold", Severity.WARNING, False, 360),
)
_BY_EVENT = {info.event: info for info in CATALOGUE}


def event_info(event: NotificationEvent) -> EventInfo:
    """The catalogue entry of ``event``."""
    return _BY_EVENT[event]


def parse_events(values: object, *, project_scoped: bool) -> list[NotificationEvent]:
    """Validate a list of event names for a rule; global-only events need the default scope."""
    if not isinstance(values, list):
        raise ValidationError("events must be a list")
    seen: list[NotificationEvent] = []
    for value in values:
        try:
            event = NotificationEvent(str(value))
        except ValueError as error:
            raise ValidationError(f"unknown event: {str(value)[:60]}") from error
        if project_scoped and not event_info(event).project_scoped:
            raise ValidationError(f"{event.value} can only be set in the default rule")
        if event not in seen:
            seen.append(event)
    return seen


class ChannelKind(StrEnum):
    """How a notification is delivered."""

    SLACK = "slack"
    WEBHOOK = "webhook"
    EMAIL = "email"


class Security(StrEnum):
    """Transport security of an SMTP connection."""

    NONE = "none"
    STARTTLS = "starttls"
    TLS = "tls"


def validate_channel_name(value: object) -> str:
    """A channel name: 1-80 printable characters without line breaks."""
    text = value.strip() if isinstance(value, str) else ""
    if not 1 <= len(text) <= MAX_NAME or not text.isprintable():
        raise ValidationError(f"a channel name has 1-{MAX_NAME} printable characters")
    return text


def _clean(value: object, field_name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValidationError(f"{field_name} is required (at most {limit} characters)")
    text = value.strip()
    if not text.isprintable():
        raise ValidationError(f"{field_name} contains invalid characters")
    return text


def _url(value: object, field_name: str, *, https_only: bool) -> str:
    text = _clean(value, field_name, MAX_URL)
    parts = urlsplit(text)
    allowed = {"https"} if https_only else {"http", "https"}
    if parts.scheme not in allowed or not parts.hostname or parts.username or parts.password:
        label = "an https" if https_only else "an http(s)"
        raise ValidationError(f"{field_name} must be {label} URL without credentials")
    return text


def _address(value: object, field_name: str) -> str:
    text = _clean(value, field_name, MAX_EMAIL)
    if not _EMAIL.match(text):
        raise ValidationError(f"{field_name} is not a valid email address")
    return text


@dataclass(frozen=True, slots=True)
class ChannelConfig:
    """A validated channel configuration.

    ``secret`` is what gets encrypted (a JSON serialisable dict); ``hint`` is the last
    characters of a webhook URL; ``summary`` holds non-secret facts the UI shows (never a URL,
    username or password).
    """

    kind: ChannelKind
    secret: dict[str, object]
    hint: str | None
    summary: dict[str, object] = field(default_factory=dict)


def _hint(url: str) -> str:
    return url[-HINT_CHARS:]


def validate_channel_config(
    kind: ChannelKind, raw: Mapping[str, object], current: Mapping[str, object] | None = None
) -> ChannelConfig:
    """Validate ``raw`` for ``kind``. With ``current`` (the stored secret) omitted fields keep
    their stored value, so a rotation can change only the URL or only the password."""
    merged: dict[str, object] = {
        **(current or {}),
        **{key: value for key, value in raw.items() if value is not None},
    }
    if kind is ChannelKind.SLACK:
        return _slack(merged)
    if kind is ChannelKind.WEBHOOK:
        return _webhook(merged)
    return _email(merged)


def _slack(merged: Mapping[str, object]) -> ChannelConfig:
    url = _url(merged.get("webhook_url"), "webhook_url", https_only=True)
    return ChannelConfig(ChannelKind.SLACK, {"webhook_url": url}, _hint(url))


def _webhook(merged: Mapping[str, object]) -> ChannelConfig:
    url = _url(merged.get("url"), "url", https_only=False)
    secret: dict[str, object] = {"url": url}
    signing = merged.get("signing_secret")
    if signing not in (None, ""):
        secret["signing_secret"] = _clean(signing, "signing_secret", MAX_SECRET)
    return ChannelConfig(
        ChannelKind.WEBHOOK, secret, _hint(url), {"signed": "signing_secret" in secret}
    )


def _email(merged: Mapping[str, object]) -> ChannelConfig:
    host = _clean(merged.get("host"), "host", 253)
    if not _HOST.match(host):
        raise ValidationError("host is not a valid host name")
    port = merged.get("port", 587)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValidationError("port must be 1-65535")
    try:
        security = Security(str(merged.get("security", "starttls")))
    except ValueError as error:
        raise ValidationError("security must be none, starttls or tls") from error
    sender = _address(merged.get("from_address"), "from_address")
    recipients = merged.get("to_addresses")
    if not isinstance(recipients, list) or not 1 <= len(recipients) <= MAX_RECIPIENTS:
        raise ValidationError(f"to_addresses needs 1-{MAX_RECIPIENTS} addresses")
    to = [_address(item, "to_addresses") for item in recipients]
    secret: dict[str, object] = {
        "host": host,
        "port": port,
        "security": security.value,
        "from_address": sender,
        "to_addresses": to,
    }
    username = merged.get("username")
    password = merged.get("password")
    if username not in (None, ""):
        secret["username"] = _clean(username, "username", MAX_SECRET)
        if password in (None, ""):
            raise ValidationError("password is required with a username")
        secret["password"] = _clean(password, "password", MAX_SECRET)
    summary: dict[str, object] = {
        "host": host,
        "port": port,
        "security": security.value,
        "from_address": sender,
        "to_addresses": to,
        "authenticated": "username" in secret,
    }
    return ChannelConfig(ChannelKind.EMAIL, secret, None, summary)


def short_sha(sha: str | None) -> str | None:
    """The 7 character form of a commit hash."""
    return sha[:SHORT_SHA] if sha else None


def clip(text: str | None, limit: int) -> str | None:
    """Cut ``text`` to ``limit`` characters, ending with an ellipsis when it was longer."""
    if text is None:
        return None
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _clip(text: str, limit: int) -> str:
    return clip(text, limit) or ""


@dataclass(frozen=True, slots=True)
class EventPayload:
    """One announcement. It carries facts only: links are paths, secrets never appear.

    ``dedupe_key`` names the resource ("run.failed:12" for environment 12); two payloads with
    the same key within the quiet period collapse into one delivery per channel.
    """

    event: NotificationEvent
    dedupe_key: str
    title: str
    project: str | None = None
    environment: str | None = None
    link_path: str = "/"
    run_id: int | None = None
    run_number: int | None = None
    deployment_id: int | None = None
    commit_sha: str | None = None
    triggered_by: str | None = None
    failed_stage: str | None = None
    failed_step: str | None = None
    message: str | None = None
    log_tail: tuple[str, ...] = ()
    details: dict[str, str] = field(default_factory=dict)

    @property
    def severity(self) -> Severity:
        """Severity of the event."""
        return event_info(self.event).severity

    def bounded(self) -> EventPayload:
        """A copy within the limits (log tail: only the last 20 lines, each cut)."""
        details = {_clip(k, 40): _clip(v, 200) for k, v in list(self.details.items())[:10]}
        return EventPayload(
            event=self.event,
            dedupe_key=_clip(self.dedupe_key, 160),
            title=_clip(self.title, MAX_TITLE),
            project=clip(self.project, 64),
            environment=self.environment,
            link_path=_clip(self.link_path, 300) or "/",
            run_id=self.run_id,
            run_number=self.run_number,
            deployment_id=self.deployment_id,
            commit_sha=short_sha(self.commit_sha),
            triggered_by=clip(self.triggered_by, 120),
            failed_stage=clip(self.failed_stage, 120),
            failed_step=clip(self.failed_step, 160),
            message=clip(self.message, MAX_TEXT),
            log_tail=tuple(_clip(line, MAX_LINE) for line in self.log_tail[-LOG_TAIL_LINES:]),
            details=details,
        )

    def to_json(self) -> dict[str, object]:
        """The stored form (always bounded)."""
        p = self.bounded()
        return {
            "event": p.event.value,
            "dedupe_key": p.dedupe_key,
            "title": p.title,
            "project": p.project,
            "environment": p.environment,
            "link_path": p.link_path,
            "run_id": p.run_id,
            "run_number": p.run_number,
            "deployment_id": p.deployment_id,
            "commit_sha": p.commit_sha,
            "triggered_by": p.triggered_by,
            "failed_stage": p.failed_stage,
            "failed_step": p.failed_step,
            "message": p.message,
            "log_tail": list(p.log_tail),
            "details": dict(p.details),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> EventPayload:
        """Parse the stored form."""

        def text(key: str) -> str | None:
            value = data.get(key)
            return value if isinstance(value, str) else None

        def number(key: str) -> int | None:
            value = data.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) else None

        tail = data.get("log_tail")
        details = data.get("details")
        return cls(
            event=NotificationEvent(str(data["event"])),
            dedupe_key=text("dedupe_key") or "",
            title=text("title") or "",
            project=text("project"),
            environment=text("environment"),
            link_path=text("link_path") or "/",
            run_id=number("run_id"),
            run_number=number("run_number"),
            deployment_id=number("deployment_id"),
            commit_sha=text("commit_sha"),
            triggered_by=text("triggered_by"),
            failed_stage=text("failed_stage"),
            failed_step=text("failed_step"),
            message=text("message"),
            log_tail=tuple(str(x) for x in tail) if isinstance(tail, list) else (),
            details={str(k): str(v) for k, v in details.items()}
            if isinstance(details, dict)
            else {},
        )


class DeliveryStatus(StrEnum):
    """State of one outbox row."""

    PENDING = "pending"
    SENDING = "sending"
    SENT = "sent"
    FAILED = "failed"  # attempts exhausted
