"""Message formats of the notification channels: Slack blocks, generic JSON, plain email.

Pure functions of an ``EventPayload``. The only link is the public origin plus the payload's
path; no format ever includes configuration or secrets.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import datetime
from email.message import EmailMessage

from backend.domain.notifications import EventPayload

SIGNATURE_HEADER = "X-Jendock-Signature"
EVENT_HEADER = "X-Jendock-Event"
DELIVERY_HEADER = "X-Jendock-Delivery"
SLACK_SECTION_LIMIT = 2900


def link(origin: str, payload: EventPayload) -> str:
    """The absolute URL of the page the message points at."""
    path = payload.link_path if payload.link_path.startswith("/") else f"/{payload.link_path}"
    return f"{origin.rstrip('/')}{path}"


def facts(payload: EventPayload) -> list[tuple[str, str]]:
    """The labelled facts of a message, in reading order."""
    where = "/".join(part for part in (payload.project, payload.environment) if part)
    items: list[tuple[str, str | None]] = [
        ("Project", where or None),
        ("Run", f"#{payload.run_number}" if payload.run_number is not None else None),
        ("Commit", payload.commit_sha),
        ("Triggered by", payload.triggered_by),
        ("Failed stage", payload.failed_stage),
        ("Failed step", payload.failed_step),
        *((key, value) for key, value in payload.details.items()),
    ]
    return [(label, value) for label, value in items if value]


def subject(payload: EventPayload) -> str:
    """A one-line subject or fallback text."""
    return f"[{payload.severity.value}] {payload.title}".replace("\r", " ").replace("\n", " ")


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_body(payload: EventPayload, origin: str) -> dict[str, object]:
    """A Slack incoming-webhook body: fallback text plus blocks."""
    payload = payload.bounded()
    head = f"*{_slack_escape(payload.title)}*"
    if payload.message:
        head += f"\n{_slack_escape(payload.message)}"
    blocks: list[dict[str, object]] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": head[:SLACK_SECTION_LIMIT]}}
    ]
    found = facts(payload)
    if found:
        fields = [
            {"type": "mrkdwn", "text": f"*{label}*\n{_slack_escape(value)}"}
            for label, value in found[:10]
        ]
        blocks.append({"type": "section", "fields": fields})
    if payload.log_tail:
        # A code block must not be closed early by the log itself.
        text = "\n".join(line.replace("```", "'''") for line in payload.log_tail)
        text = _slack_escape(text)[-(SLACK_SECTION_LIMIT - 20) :]
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"```{text}```"}})
    url = link(origin, payload)
    blocks.append(
        {"type": "section", "text": {"type": "mrkdwn", "text": f"<{url}|Open in Jendock>"}}
    )
    return {"text": subject(payload), "blocks": blocks}


def webhook_body(
    payload: EventPayload, origin: str, delivery_id: int, now: datetime
) -> dict[str, object]:
    """The generic JSON body. ``text`` and ``content`` repeat the summary so Microsoft Teams
    and Discord style endpoints show something useful without a custom mapping."""
    payload = payload.bounded()
    lines = [subject(payload), *(f"{label}: {value}" for label, value in facts(payload))]
    if payload.message:
        lines.insert(1, payload.message)
    lines.append(link(origin, payload))
    text = "\n".join(lines)
    return {
        "event": payload.event.value,
        "severity": payload.severity.value,
        "title": payload.title,
        "text": text,
        "content": text,
        "project": payload.project,
        "environment": payload.environment,
        "run_id": payload.run_id,
        "run_number": payload.run_number,
        "deployment_id": payload.deployment_id,
        "commit": payload.commit_sha,
        "triggered_by": payload.triggered_by,
        "failed_stage": payload.failed_stage,
        "failed_step": payload.failed_step,
        "message": payload.message,
        "log_tail": list(payload.log_tail),
        "details": dict(payload.details),
        "url": link(origin, payload),
        "delivery_id": delivery_id,
        "timestamp": now.isoformat(),
    }


def signature(secret: str, body: bytes) -> str:
    """``sha256=<hex>``: the HMAC-SHA256 of the exact request body under the signing secret."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def email_text(payload: EventPayload, origin: str) -> str:
    """The plain-text email body."""
    payload = payload.bounded()
    lines = [payload.title, ""]
    if payload.message:
        lines += [payload.message, ""]
    lines += [f"{label}: {value}" for label, value in facts(payload)]
    if payload.log_tail:
        lines += ["", f"Last {len(payload.log_tail)} lines of the failing step:", ""]
        lines += [f"  {line}" for line in payload.log_tail]
    lines += ["", f"Open in Jendock: {link(origin, payload)}", ""]
    return "\n".join(lines)


def email_message(
    payload: EventPayload, origin: str, sender: str, recipients: list[str]
) -> EmailMessage:
    """The email, with CR/LF stripped from every header value."""
    message = EmailMessage()
    message["Subject"] = subject(payload)
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["X-Jendock-Event"] = payload.event.value
    message.set_content(email_text(payload, origin))
    return message
