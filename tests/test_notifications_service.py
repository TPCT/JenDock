"""Channels, rules, the outbox (dedupe) and the dispatcher (retry with backoff)."""

from __future__ import annotations

import json
import logging
from datetime import timedelta

import pytest
from sqlalchemy import Engine

from backend.adapters.notify_senders import DeliveryError
from backend.domain.enums import EnvironmentName
from backend.domain.errors import ConflictError, NotFoundError, ValidationError
from backend.domain.notifications import ChannelKind, DeliveryStatus, NotificationEvent
from backend.repositories.notifications import NotificationRepository
from backend.services.audit import AuditService
from backend.services.notification_channels import decrypt_config
from backend.services.notification_dispatcher import DispatchPolicy, backoff
from backend.services.notification_outbox import OutboxEmitter
from backend.services.notification_rules import (
    NotificationQueryService,
    RuleInput,
    RuleScope,
)
from backend.services.secret_cipher import SecretCipher
from tests.api_support import FakeClock
from tests.notification_support import (
    ACTOR,
    SIGNING_SECRET,
    SLACK_URL,
    SMTP_PASSWORD,
    World,
    payload,
)

DEV = EnvironmentName.DEVELOPMENT
PROD = EnvironmentName.PRODUCTION
FAILED = NotificationEvent.RUN_FAILED
FIXED = NotificationEvent.RUN_FIXED


@pytest.fixture
def world(engine: Engine, cipher: SecretCipher, clock: FakeClock, project: str) -> World:
    return World(engine, cipher, clock)


# --- channels and secrets ----------------------------------------------------------------------


def test_channel_secrets_are_encrypted_and_only_the_last_four_characters_are_exposed(
    world: World,
) -> None:
    channel_id = world.slack()
    with world.uow() as uow:
        raw = NotificationRepository(uow.session).get_channel_with_config(channel_id)
        assert raw is not None
        assert SLACK_URL.encode() not in raw.config_ciphertext
        assert decrypt_config(world.cipher, raw) == {"webhook_url": SLACK_URL}
        assert raw.hint == SLACK_URL[-4:] and raw.summary == {}
        assert SLACK_URL not in json.dumps([raw.hint, raw.summary, raw.name, raw.kind])


def test_email_credentials_are_encrypted_and_the_summary_has_no_username_or_password(
    world: World,
) -> None:
    channel_id = world.email()
    with world.uow() as uow:
        raw = NotificationRepository(uow.session).get_channel_with_config(channel_id)
        assert raw is not None
        assert SMTP_PASSWORD.encode() not in raw.config_ciphertext
        stored = decrypt_config(world.cipher, raw)
        assert stored["password"] == SMTP_PASSWORD and stored["username"] == "mailer"
        assert raw.summary["authenticated"] is True
        assert SMTP_PASSWORD not in json.dumps(raw.summary) and "mailer" not in json.dumps(
            raw.summary
        )


def test_audit_records_channel_changes_without_urls_or_credentials(world: World) -> None:
    channel_id = world.webhook(signing=SIGNING_SECRET)
    world.channels().update(
        channel_id, ACTOR, name="renamed", config={"signing_secret": "rotated-secret"}
    )
    world.channels().test(channel_id, ACTOR)
    world.channels().delete(channel_id, ACTOR)
    events = AuditService(world.uow()).list(limit=50)
    actions = [e.action for e in events if e.action.startswith("notification.")]
    assert sorted(actions) == [
        "notification.channel.create",
        "notification.channel.delete",
        "notification.channel.test",
        "notification.channel.update",
    ]
    dump = json.dumps([[e.action, e.target_id, e.details] for e in events])
    for secret in (SIGNING_SECRET, "rotated-secret", "hooks.example.test", "abcd1234"):
        assert secret not in dump
    update = next(e for e in events if e.action == "notification.channel.update")
    assert update.details["changed"] == ["name", "signing_secret"]


def test_updating_one_field_keeps_the_others(world: World) -> None:
    channel_id = world.webhook(signing=SIGNING_SECRET)
    world.channels().update(channel_id, ACTOR, config={"url": "https://hooks.example.test/new9"})
    with world.uow() as uow:
        raw = NotificationRepository(uow.session).get_channel_with_config(channel_id)
        assert raw is not None
        assert decrypt_config(world.cipher, raw) == {
            "url": "https://hooks.example.test/new9",
            "signing_secret": SIGNING_SECRET,
        }
        assert raw.hint == "new9"


def test_names_are_unique_validation_applies_and_a_missing_channel_is_not_found(
    world: World,
) -> None:
    world.slack("one")
    with pytest.raises(ConflictError):
        world.slack("one")
    with pytest.raises(ValidationError):
        world.channels().create("bad", ChannelKind.SLACK, {"webhook_url": "http://x.test/y"}, ACTOR)
    with pytest.raises(NotFoundError):
        world.channels().update(999, ACTOR, enabled=False)
    with pytest.raises(NotFoundError):
        world.channels().delete(999, ACTOR)
    with pytest.raises(NotFoundError):
        world.channels().test(999, ACTOR)


def test_send_test_delivers_records_history_and_reports_failures_safely(world: World) -> None:
    channel_id = world.slack()
    ok, error = world.channels().test(channel_id, ACTOR)
    assert (ok, error) == (True, None)
    [call] = world.poster.calls
    assert call.url == SLACK_URL and "Test notification from Jendock" in call.body.decode()

    world.poster.script = [403]
    ok, error = world.channels().test(channel_id, ACTOR)
    assert ok is False and error == "the endpoint answered HTTP 403"
    world.poster.script = [DeliveryError("request failed (URLError)")]
    ok, error = world.channels().test(channel_id, ACTOR)
    assert error == "request failed (URLError)"

    rows = world.deliveries()
    assert [r.status for r in rows] == ["sent", "failed", "failed"]
    assert all(r.is_test for r in rows)
    assert SLACK_URL not in json.dumps([r.last_error for r in rows])
    with world.uow() as uow:
        channel = NotificationRepository(uow.session).get_channel(channel_id)
        assert channel is not None and channel.last_test_ok is False


def test_deleting_a_channel_removes_its_rules_and_history(world: World) -> None:
    channel_id = world.slack()
    world.subscribe([channel_id], [FAILED])
    world.emitter.emit(payload())
    assert len(world.deliveries()) == 1
    world.channels().delete(channel_id, ACTOR)
    assert world.deliveries() == []
    assert world.rules().get(RuleScope()).rules == []


# --- rules --------------------------------------------------------------------------------------


def test_the_most_specific_scope_with_rules_decides(world: World) -> None:
    default = world.slack("default")
    project = world.slack("project")
    environment = world.slack("env")
    world.subscribe([default], [FAILED])
    emitter = world.emitter

    def targets(env: EnvironmentName, key: str) -> list[str]:
        before = len(world.deliveries())
        emitter.emit(payload(key=key, environment=env.value))
        names = {c.id: c.name for c in NotificationQueryService(world.uow()).channels()}
        return [names[r.channel_id] for r in world.deliveries()[before:]]

    assert targets(DEV, "a") == ["default"]
    world.subscribe([project], [FAILED], RuleScope("demo"))
    assert targets(DEV, "b") == ["project"]
    world.subscribe([environment], [FAILED], RuleScope("demo", DEV))
    assert targets(DEV, "c") == ["env"]
    assert targets(PROD, "d") == ["project"]  # production has no rules of its own
    world.subscribe([], [], RuleScope("demo", DEV))
    assert targets(DEV, "e") == ["project"]  # an empty set removes the override


def test_an_empty_event_list_or_a_disabled_rule_mutes_a_channel_for_that_scope(
    world: World,
) -> None:
    channel = world.slack()
    world.subscribe([channel], [FAILED])
    world.rules().replace(RuleScope("demo"), [RuleInput(channel, [], True)], ACTOR)
    assert world.emitter.emit(payload()) == 0
    world.rules().replace(RuleScope("demo"), [RuleInput(channel, [FAILED.value], False)], ACTOR)
    assert world.emitter.emit(payload(key="2")) == 0
    world.rules().replace(RuleScope("demo"), [RuleInput(channel, [FAILED.value], True)], ACTOR)
    assert world.emitter.emit(payload(key="3")) == 1


def test_only_subscribed_events_and_enabled_channels_are_queued(world: World) -> None:
    on, off = world.slack("on"), world.slack("off")
    world.subscribe([on, off], [FAILED])
    world.channels().update(off, ACTOR, enabled=False)
    assert world.emitter.emit(payload(FIXED, title="fixed")) == 0
    assert world.emitter.emit(payload()) == 1
    [row] = world.deliveries()
    assert row.channel_id == on and row.status == "pending"
    assert row.project_id is not None


def test_global_events_follow_the_default_rule_only(world: World) -> None:
    default, project = world.slack("default"), world.slack("project")
    world.subscribe([default], [NotificationEvent.DISK_USAGE_HIGH])
    world.subscribe([project], [FAILED], RuleScope("demo"))
    disk = payload(NotificationEvent.DISK_USAGE_HIGH, key="data", project=None, environment=None)
    assert world.emitter.emit(disk) == 1
    assert world.deliveries()[0].channel_id == default
    assert world.deliveries()[0].project_id is None


def test_rule_validation_and_audit(world: World) -> None:
    channel = world.slack()
    with pytest.raises(ValidationError, match="default rule"):
        world.subscribe([channel], [NotificationEvent.DISK_USAGE_HIGH], RuleScope("demo"))
    with pytest.raises(ValidationError, match="only once"):
        world.rules().replace(RuleScope(), [RuleInput(channel, []), RuleInput(channel, [])], ACTOR)
    with pytest.raises(NotFoundError):
        world.rules().replace(RuleScope(), [RuleInput(404, [])], ACTOR)
    with pytest.raises(NotFoundError):
        world.rules().get(RuleScope("missing"))
    world.subscribe([channel], [FAILED, FIXED], RuleScope("demo", PROD))
    view = world.rules().get(RuleScope("demo", PROD))
    assert view.scope == "environment" and view.rules[0].events == ["run.failed", "run.fixed"]
    empty = world.rules().get(RuleScope("demo", DEV))
    assert empty.inherited_from is None
    world.subscribe([channel], [FAILED])
    assert world.rules().get(RuleScope("demo", DEV)).inherited_from == "default"
    world.subscribe([channel], [FAILED], RuleScope("demo"))
    assert world.rules().get(RuleScope("demo", DEV)).inherited_from == "project"
    [event, *_] = [
        e
        for e in AuditService(world.uow()).list(limit=50)
        if e.action == "notification.rules.update"
    ]
    assert event.details["scope"] == "project" and event.details["project"] == "demo"


# --- quiet duplicates ---------------------------------------------------------------------------


def test_the_same_event_and_resource_within_the_quiet_period_collapses(
    world: World, clock: FakeClock
) -> None:
    channel = world.slack()
    world.subscribe([channel], [FAILED])
    emitter = world.emitter
    assert emitter.emit(payload(key="env1")) == 1
    clock.advance(minutes=29)
    assert emitter.emit(payload(key="env1")) == 0
    assert emitter.emit(payload(key="env1")) == 0
    assert emitter.emit(payload(key="env2")) == 1  # another resource is not a duplicate
    clock.advance(minutes=2)  # 31 minutes after the first
    assert emitter.emit(payload(key="env1")) == 1
    first = world.deliveries()[0]
    assert first.suppressed == 2
    assert len(world.deliveries()) == 3


def test_dedupe_is_per_channel_and_test_sends_do_not_count(world: World) -> None:
    one, two = world.slack("one"), world.slack("two", url="https://hooks.slack.test/b/c/dddd")
    world.subscribe([one], [FAILED])
    world.channels().test(one, ACTOR)  # a test row must not suppress a real event
    assert world.emitter.emit(payload()) == 1
    world.subscribe([one, two], [FAILED])
    assert world.emitter.emit(payload()) == 1  # only the new channel gets it
    assert [r.channel_id for r in world.deliveries() if not r.is_test] == [one, two]


def test_certificate_events_use_a_longer_quiet_period_than_the_default(
    world: World, clock: FakeClock
) -> None:
    channel = world.slack()
    world.subscribe([channel], [NotificationEvent.CERTIFICATE_EXPIRING])
    expiring = payload(NotificationEvent.CERTIFICATE_EXPIRING, key="7")
    assert world.emitter.emit(expiring) == 1
    clock.advance(hours=23)
    assert world.emitter.emit(expiring) == 0
    clock.advance(hours=2)
    assert world.emitter.emit(expiring) == 1


def test_emit_never_raises_and_never_touches_the_network(
    world: World, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    channel = world.slack()
    world.subscribe([channel], [FAILED])
    world.emitter.emit(payload())
    assert world.poster.calls == []  # queued only: the dispatcher sends later

    def broken(*_args: object) -> int:
        raise RuntimeError("database is gone")

    monkeypatch.setattr(OutboxEmitter, "_emit", broken)
    with caplog.at_level(logging.ERROR):
        assert world.emitter.emit(payload(key="other")) == 0
    assert "notification could not be queued" in caplog.text


# --- dispatcher: delivery, retry, backoff -------------------------------------------------------


def test_backoff_doubles_and_is_capped_at_one_hour() -> None:
    delays = [backoff(n, 30.0).total_seconds() for n in range(1, 10)]
    assert delays == [30, 60, 120, 240, 480, 960, 1920, 3600, 3600]


def test_a_queued_message_is_delivered_once_and_marked_sent(world: World, clock: FakeClock) -> None:
    channel = world.slack()
    world.subscribe([channel], [FAILED])
    world.emitter.emit(payload(log_tail=("a", "b")))
    assert world.dispatcher.run_once() == 1
    [call] = world.poster.calls
    assert call.url == SLACK_URL and "Open in Jendock" in call.body.decode()
    [row] = world.deliveries()
    assert (row.status, row.attempts, row.last_error) == ("sent", 1, None)
    assert row.delivered_at == clock()
    assert world.dispatcher.run_once() == 0  # nothing left to do


def test_failed_attempts_are_retried_with_exponential_backoff_until_they_succeed(
    world: World, clock: FakeClock
) -> None:
    channel = world.slack()
    world.subscribe([channel], [FAILED])
    world.emitter.emit(payload())
    world.poster.script = [500, DeliveryError("request failed (URLError)"), 502, 200]
    dispatcher = world.dispatcher
    start = clock()

    assert dispatcher.run_once() == 1  # attempt 1 fails -> retry in 30 s
    [row] = world.deliveries()
    assert row.status == "pending" and row.attempts == 1
    assert row.last_error == "the endpoint answered HTTP 500"
    assert row.next_attempt_at == start + timedelta(seconds=30)
    assert dispatcher.run_once() == 0  # not due yet

    clock.advance(seconds=30)
    assert dispatcher.run_once() == 1  # attempt 2 -> retry in 60 s
    [row] = world.deliveries()
    assert row.attempts == 2 and row.next_attempt_at == clock() + timedelta(seconds=60)
    assert row.last_error == "request failed (URLError)"

    clock.advance(seconds=59)
    assert dispatcher.run_once() == 0
    clock.advance(seconds=1)
    assert dispatcher.run_once() == 1  # attempt 3 -> retry in 120 s
    [row] = world.deliveries()
    assert row.attempts == 3 and row.next_attempt_at == clock() + timedelta(seconds=120)

    clock.advance(seconds=120)
    assert dispatcher.run_once() == 1  # attempt 4 succeeds
    [row] = world.deliveries()
    assert (row.status, row.attempts, row.last_error) == ("sent", 4, None)
    assert len(world.poster.calls) == 4


def test_a_message_that_keeps_failing_ends_as_failed_with_the_last_error(
    world: World, clock: FakeClock
) -> None:
    world.policy = DispatchPolicy(max_attempts=3, retry_base_seconds=10.0)
    channel = world.slack()
    world.subscribe([channel], [FAILED])
    world.emitter.emit(payload())
    world.poster.script = [500, 500, 503]
    dispatcher = world.dispatcher
    for _ in range(3):
        dispatcher.run_once()
        clock.advance(hours=1)
    [row] = world.deliveries()
    assert (row.status, row.attempts) == ("failed", 3)
    assert row.last_error == "the endpoint answered HTTP 503"
    clock.advance(days=1)
    assert dispatcher.run_once() == 0  # exhausted rows are not retried again
    assert len(world.poster.calls) == 3


def test_a_row_claimed_by_a_dead_worker_is_picked_up_again_after_the_stale_period(
    world: World, clock: FakeClock
) -> None:
    channel = world.slack()
    world.subscribe([channel], [FAILED])
    world.emitter.emit(payload())
    with world.uow() as uow:
        row = NotificationRepository(uow.session).get_delivery(world.deliveries()[0].id)
        assert row is not None
        row.status, row.attempts, row.claimed_at = "sending", 1, clock()  # worker died here
    assert world.dispatcher.run_once() == 0
    clock.advance(seconds=301)
    assert world.dispatcher.run_once() == 1
    [done] = world.deliveries()
    assert done.status == "sent" and done.attempts == 2


def test_unusable_channels_fail_permanently_without_a_network_call(
    world: World, cipher: SecretCipher
) -> None:
    channel = world.slack()
    world.subscribe([channel], [FAILED])
    world.emitter.emit(payload())
    world.channels().update(channel, ACTOR, enabled=False)
    assert world.dispatcher.run_once() == 1
    [row] = world.deliveries()
    assert row.status == "failed" and row.last_error == "the channel is disabled"
    assert world.poster.calls == []


def test_email_delivery_uses_the_mailer_and_a_mailer_failure_is_retried(
    world: World, clock: FakeClock
) -> None:
    channel = world.email()
    world.subscribe([channel], [FAILED])
    world.emitter.emit(payload(log_tail=("x",)))
    world.mailer.error = DeliveryError("SMTP authentication failed")
    world.dispatcher.run_once()
    [row] = world.deliveries()
    assert row.status == "pending" and row.last_error == "SMTP authentication failed"
    world.mailer.error = None
    clock.advance(seconds=30)
    world.dispatcher.run_once()
    [(config, message)] = world.mailer.sent
    assert config["password"] == SMTP_PASSWORD  # the secret reaches only the SMTP client
    assert message["To"] == "ops@example.test, dev@example.test"
    assert SMTP_PASSWORD not in message.as_string()


def test_old_finished_deliveries_are_pruned(world: World, clock: FakeClock) -> None:
    channel = world.slack()
    world.subscribe([channel], [FAILED])
    world.emitter.emit(payload())
    dispatcher = world.dispatcher
    dispatcher.run_once()
    clock.advance(days=31)
    world.emitter.emit(payload(key="new"))
    dispatcher.prune()  # drops rows older than the history window
    assert [r.dedupe_key for r in world.deliveries()] == ["run.failed:new"]


def test_status_values_are_the_documented_ones() -> None:
    assert [s.value for s in DeliveryStatus] == ["pending", "sending", "sent", "failed"]
