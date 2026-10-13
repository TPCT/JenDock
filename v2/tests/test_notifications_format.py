"""Notification vocabulary, validation and message formats (pure, no database)."""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime

import pytest

from backend.adapters.notify_format import (
    email_message,
    link,
    signature,
    slack_body,
    webhook_body,
)
from backend.adapters.notify_senders import Deliverer, DeliveryError
from backend.domain.errors import ValidationError
from backend.domain.notifications import (
    CATALOGUE,
    ChannelKind,
    EventPayload,
    NotificationEvent,
    Severity,
    parse_events,
    validate_channel_config,
    validate_channel_name,
)
from tests.notification_support import ORIGIN, FakeMailer, FakePoster, payload

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def test_catalogue_covers_every_event_once_with_the_specified_severities() -> None:
    assert [i.event for i in CATALOGUE] == list(NotificationEvent)
    severity = {i.event: i.severity for i in CATALOGUE}
    assert severity[NotificationEvent.DEPLOYMENT_ROLLED_BACK] is Severity.CRITICAL
    assert severity[NotificationEvent.DEPLOYMENT_ROLLBACK_FAILED] is Severity.CRITICAL
    assert severity[NotificationEvent.DEPLOYMENT_SUCCEEDED] is Severity.INFO


def test_global_events_cannot_be_subscribed_in_a_project_scope() -> None:
    assert parse_events(["disk.usage_high"], project_scoped=False)
    with pytest.raises(ValidationError, match="default rule"):
        parse_events(["disk.usage_high"], project_scoped=True)
    with pytest.raises(ValidationError, match="unknown event"):
        parse_events(["nope"], project_scoped=False)
    assert parse_events(["run.failed", "run.failed"], project_scoped=True) == [
        NotificationEvent.RUN_FAILED
    ]


def test_slack_config_needs_https_and_keeps_the_last_four_characters_as_hint() -> None:
    config = validate_channel_config(
        ChannelKind.SLACK, {"webhook_url": "https://hooks.slack.test/services/AAA/BBB/wxyz"}
    )
    assert config.hint == "wxyz"
    assert config.summary == {}
    for bad in ("http://hooks.slack.test/x", "https://user:pw@hooks.slack.test/x", "nonsense", ""):
        with pytest.raises(ValidationError):
            validate_channel_config(ChannelKind.SLACK, {"webhook_url": bad})


def test_webhook_config_allows_http_and_an_optional_signing_secret() -> None:
    plain = validate_channel_config(ChannelKind.WEBHOOK, {"url": "http://hooks.test/in/1234"})
    assert plain.summary == {"signed": False} and "signing_secret" not in plain.secret
    signed = validate_channel_config(
        ChannelKind.WEBHOOK, {"url": "https://h.test/9999", "signing_secret": "s3"}
    )
    assert signed.summary == {"signed": True} and signed.secret["signing_secret"] == "s3"
    with pytest.raises(ValidationError):
        validate_channel_config(ChannelKind.WEBHOOK, {"url": "ftp://h.test/x"})


def test_a_partial_update_merges_with_the_stored_configuration() -> None:
    stored = {"url": "https://h.test/aaaa", "signing_secret": "old"}
    rotated = validate_channel_config(ChannelKind.WEBHOOK, {"signing_secret": "new"}, stored)
    assert rotated.secret == {"url": "https://h.test/aaaa", "signing_secret": "new"}
    moved = validate_channel_config(ChannelKind.WEBHOOK, {"url": "https://h.test/bbbb"}, stored)
    assert moved.secret["signing_secret"] == "old" and moved.hint == "bbbb"


def test_email_config_is_validated_and_the_summary_leaves_out_credentials() -> None:
    config = validate_channel_config(
        ChannelKind.EMAIL,
        {
            "host": "smtp.example.test",
            "port": 465,
            "security": "tls",
            "username": "u",
            "password": "p",
            "from_address": "a@example.test",
            "to_addresses": ["b@example.test"],
        },
    )
    assert config.summary["authenticated"] is True
    assert "username" not in config.summary and "password" not in config.summary
    assert config.hint is None
    base = {"host": "h.test", "from_address": "a@example.test", "to_addresses": ["b@example.test"]}
    changes: list[dict[str, object]] = [
        {"port": 0},
        {"port": "25"},
        {"security": "ssl"},
        {"to_addresses": []},
        {"to_addresses": ["not-an-address"]},
        {"to_addresses": ["a@example.test\nBcc: x@y.test"]},
        {"from_address": "bad"},
        {"username": "u"},  # a username needs a password
        {"host": "bad host"},
    ]
    for change in changes:
        with pytest.raises(ValidationError):
            validate_channel_config(ChannelKind.EMAIL, {**base, **change})
    assert validate_channel_config(ChannelKind.EMAIL, base).secret["port"] == 587


def test_channel_names_are_short_printable_text() -> None:
    assert validate_channel_name("  #alerts ") == "#alerts"
    for bad in ("", "x" * 81, "line\nbreak", 5):
        with pytest.raises(ValidationError):
            validate_channel_name(bad)


def test_payloads_are_bounded_to_the_last_20_log_lines_and_a_short_sha() -> None:
    lines = tuple(f"line {n} " + "x" * 400 for n in range(60))
    bounded = payload(log_tail=lines, message="m" * 900).bounded()
    assert len(bounded.log_tail) == 20
    assert bounded.log_tail[0].startswith("line 40 ")
    assert all(len(line) <= 240 for line in bounded.log_tail)
    assert bounded.commit_sha == "0123456"
    assert bounded.message is not None and len(bounded.message) <= 500
    stored = bounded.to_json()
    assert EventPayload.from_json(stored).to_json() == stored
    assert len(json.dumps(stored)) < 8192


def test_links_use_the_public_origin() -> None:
    assert link(ORIGIN + "/", payload()) == f"{ORIGIN}/runs/3"


def test_slack_message_has_link_commit_trigger_failing_stage_and_escapes_markup() -> None:
    body = slack_body(
        payload(
            message="boom <@channel> & more",
            log_tail=("step 1", "```break out```", "<script>"),
        ),
        ORIGIN,
    )
    text = json.dumps(body)
    assert body["text"] == "[critical] Run #3 failed: demo/development"
    assert f"<{ORIGIN}/runs/3|Open in Jendock>" in text
    for fact in ("0123456", "admin (manual)", "Build", "sh: make", "demo/development"):
        assert fact in text
    assert "&lt;@channel&gt; &amp; more" in text
    assert "<script>" not in text and "```break" not in text  # neither markup nor fence escapes


def test_webhook_body_is_generic_json_with_text_and_content_for_teams_and_discord() -> None:
    body = webhook_body(payload(log_tail=("a", "b")), ORIGIN, 42, NOW)
    assert body["event"] == "run.failed" and body["severity"] == "critical"
    assert body["text"] == body["content"]
    assert body["url"] == f"{ORIGIN}/runs/3"
    assert body["commit"] == "0123456" and body["failed_stage"] == "Build"
    assert body["log_tail"] == ["a", "b"] and body["delivery_id"] == 42
    assert body["timestamp"] == "2026-10-13T12:00:00+00:00"


def test_signature_is_the_hmac_sha256_of_the_exact_body() -> None:
    body = b'{"a": 1}'
    expected = hmac.new(b"key", body, hashlib.sha256).hexdigest()
    assert signature("key", body) == f"sha256={expected}"


def test_email_has_subject_recipients_link_and_the_tail_of_the_failing_step() -> None:
    message = email_message(
        payload(log_tail=("one", "two")), ORIGIN, "jendock@example.test", ["a@x.test", "b@x.test"]
    )
    assert message["Subject"] == "[critical] Run #3 failed: demo/development"
    assert message["To"] == "a@x.test, b@x.test" and message["From"] == "jendock@example.test"
    body = message.get_content()
    assert f"Open in Jendock: {ORIGIN}/runs/3" in body
    assert "Last 2 lines of the failing step" in body and "  two" in body
    assert "Commit: 0123456" in body and "Triggered by: admin (manual)" in body


def test_deliverer_signs_only_when_a_signing_secret_is_configured() -> None:
    poster, mailer = FakePoster(), FakeMailer()
    deliverer = Deliverer(poster, mailer, ORIGIN, 5.0)
    deliverer.deliver(ChannelKind.WEBHOOK, {"url": "https://h.test/1"}, payload(), 7, NOW)
    deliverer.deliver(
        ChannelKind.WEBHOOK, {"url": "https://h.test/2", "signing_secret": "k"}, payload(), 8, NOW
    )
    unsigned, signed = poster.calls
    assert "X-Jendock-Signature" not in unsigned.headers
    assert signed.headers["X-Jendock-Signature"] == signature("k", signed.body)
    assert signed.headers["X-Jendock-Event"] == "run.failed"
    assert signed.headers["X-Jendock-Delivery"] == "8"
    assert signed.headers["Content-Type"] == "application/json"


def test_non_2xx_answers_are_delivery_errors_without_the_url() -> None:
    poster = FakePoster()
    poster.script = [500]
    deliverer = Deliverer(poster, FakeMailer(), ORIGIN, 5.0)
    url = "https://hooks.slack.test/services/SECRETPART"
    with pytest.raises(DeliveryError) as raised:
        deliverer.deliver(ChannelKind.SLACK, {"webhook_url": url}, payload(), 1, NOW)
    assert str(raised.value) == "the endpoint answered HTTP 500"
    assert "SECRETPART" not in str(raised.value)


def test_email_goes_through_the_mailer_with_the_stored_smtp_settings() -> None:
    mailer = FakeMailer()
    deliverer = Deliverer(FakePoster(), mailer, ORIGIN, 5.0)
    config = {
        "host": "smtp.example.test",
        "port": 587,
        "security": "starttls",
        "from_address": "jendock@example.test",
        "to_addresses": ["ops@example.test"],
    }
    deliverer.deliver(ChannelKind.EMAIL, config, payload(), 1, NOW)
    [(used, message)] = mailer.sent
    assert used["host"] == "smtp.example.test" and message["To"] == "ops@example.test"
