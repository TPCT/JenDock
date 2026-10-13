"""Notification endpoints through a real worker RPC server: secrets stay write-only."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from backend.api.container import AppContainer
from backend.domain.notifications import NotificationEvent
from backend.repositories.notifications import NotificationRepository
from backend.services.audit import AuditService
from backend.services.notification_outbox import OutboxEmitter
from tests.api_support import (
    FakeClock,
    create_user,
    login,
    make_client,
    make_container,
    make_settings,
    mutation_headers,
)
from tests.notification_support import (
    SIGNING_SECRET,
    SLACK_URL,
    SMTP_PASSWORD,
    WEBHOOK_URL,
    FakeMailer,
    FakePoster,
    payload,
    running_notification_worker,
)

CHANNELS = "/api/notifications/channels"
RULES = "/api/notifications/rules"
DEV_RULES = "/api/projects/demo/environments/development/notifications/rules"
SECRETS = (SLACK_URL, WEBHOOK_URL, SIGNING_SECRET, SMTP_PASSWORD, "XXXXsecretXXXX", "abcd1234")


@pytest.fixture
def poster() -> FakePoster:
    return FakePoster()


@pytest.fixture
def mailer() -> FakeMailer:
    return FakeMailer()


@pytest.fixture
def wired(
    tmp_path: Path,
    socket_dir: Path,
    engine: Engine,
    clock: FakeClock,
    project: str,
    poster: FakePoster,
    mailer: FakeMailer,
) -> Iterator[AppContainer]:
    key_file = tmp_path / "worker-only" / "secret.key"
    with running_notification_worker(socket_dir, engine, key_file, poster, mailer) as socket:
        yield make_container(make_settings(tmp_path, socket_path=socket), engine, clock)


@pytest.fixture
def session_client(wired: AppContainer) -> tuple[TestClient, str]:
    create_user(wired)
    client = make_client(wired)
    return client, login(client)


def post(client: TestClient, csrf: str, url: str, body: object = None) -> httpx2.Response:
    return client.post(url, json=body, headers=mutation_headers(client, csrf))


def slack_body(name: str = "alerts", url: str = SLACK_URL) -> dict[str, object]:
    return {"name": name, "kind": "slack", "config": {"webhook_url": url}}


def email_body(name: str = "mail") -> dict[str, object]:
    config = {
        "host": "smtp.example.test",
        "port": 587,
        "security": "starttls",
        "username": "mailer",
        "password": SMTP_PASSWORD,
        "from_address": "jendock@example.test",
        "to_addresses": ["ops@example.test"],
    }
    return {"name": name, "kind": "email", "config": config}


def assert_no_secret(text: str) -> None:
    for secret in SECRETS:
        assert secret not in text, f"{secret!r} leaked"


# --- channels: write-only secrets ---------------------------------------------------------------


def test_creating_a_channel_never_returns_or_logs_the_url_and_audits_without_it(
    wired: AppContainer,
    session_client: tuple[TestClient, str],
    engine: Engine,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client, csrf = session_client
    caplog.set_level(logging.DEBUG)
    created = post(client, csrf, CHANNELS, slack_body())
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["kind"] == "slack" and body["enabled"] is True and body["configured"] is True
    assert body["hint"] == "XXXX" and body["summary"] == {}
    assert_no_secret(created.text)

    listed = client.get(CHANNELS)
    assert [c["name"] for c in listed.json()] == ["alerts"]
    assert_no_secret(listed.text)
    one = client.patch(
        f"{CHANNELS}/{body['id']}",
        json={"name": "renamed"},
        headers=mutation_headers(client, csrf),
    )
    assert one.json()["name"] == "renamed" and one.json()["hint"] == "XXXX"
    assert_no_secret(one.text)

    events = AuditService(wired.uow()).list(limit=50)
    assert [e.action for e in events if e.action.startswith("notification.")] == [
        "notification.channel.update",
        "notification.channel.create",
    ]
    assert_no_secret(json.dumps([e.details for e in events]))
    assert_no_secret(caplog.text)
    with wired.uow() as uow:
        raw = NotificationRepository(uow.session).get_channel_with_config(body["id"])
        assert raw is not None and SLACK_URL.encode() not in raw.config_ciphertext


def test_email_credentials_are_write_only_and_can_be_rotated(
    wired: AppContainer, session_client: tuple[TestClient, str], mailer: FakeMailer
) -> None:
    client, csrf = session_client
    created = post(client, csrf, CHANNELS, email_body()).json()
    assert created["hint"] is None
    assert created["summary"] == {
        "host": "smtp.example.test",
        "port": 587,
        "security": "starttls",
        "from_address": "jendock@example.test",
        "to_addresses": ["ops@example.test"],
        "authenticated": True,
    }
    assert_no_secret(json.dumps(created))
    assert "mailer" not in json.dumps(created)
    rotated = client.patch(
        f"{CHANNELS}/{created['id']}",
        json={"config": {"password": "new-password-value"}},
        headers=mutation_headers(client, csrf),
    )
    assert rotated.status_code == 200 and "new-password-value" not in rotated.text
    sent = post(client, csrf, f"{CHANNELS}/{created['id']}/test")
    assert sent.json() == {"ok": True, "error": None}
    [(config, message)] = mailer.sent
    assert config["password"] == "new-password-value" and config["username"] == "mailer"
    assert message["To"] == "ops@example.test"


def test_validation_errors_are_422_and_never_echo_the_input(
    session_client: tuple[TestClient, str],
) -> None:
    client, csrf = session_client
    for body in (
        slack_body(url="http://insecure.test/TOPSECRETPATH"),
        {"name": "x", "kind": "slack", "config": {"webhook_url": 5}},
        {"name": "x", "kind": "carrier-pigeon", "config": {}},
        {"name": "x", "kind": "slack", "config": {"webhook_url": SLACK_URL}, "extra": 1},
    ):
        response = post(client, csrf, CHANNELS, body)
        assert response.status_code == 422, response.text
        assert "TOPSECRETPATH" not in response.text and SLACK_URL not in response.text
    assert client.get(CHANNELS).json() == []


def test_names_are_unique_and_unknown_channels_are_404(
    session_client: tuple[TestClient, str],
) -> None:
    client, csrf = session_client
    assert post(client, csrf, CHANNELS, slack_body()).status_code == 201
    assert post(client, csrf, CHANNELS, slack_body()).status_code == 409
    assert post(client, csrf, f"{CHANNELS}/99/test").status_code == 404
    missing = client.patch(
        f"{CHANNELS}/99", json={"enabled": False}, headers=mutation_headers(client, csrf)
    )
    assert missing.status_code == 404
    assert (
        client.delete(f"{CHANNELS}/99", headers=mutation_headers(client, csrf)).status_code == 404
    )


def test_send_test_records_a_delivery_and_reports_failures_without_the_url(
    session_client: tuple[TestClient, str], poster: FakePoster
) -> None:
    client, csrf = session_client
    channel = post(client, csrf, CHANNELS, slack_body()).json()
    ok = post(client, csrf, f"{CHANNELS}/{channel['id']}/test")
    assert ok.json() == {"ok": True, "error": None}
    assert poster.calls[0].url == SLACK_URL
    poster.script = [404]
    failed = post(client, csrf, f"{CHANNELS}/{channel['id']}/test")
    assert failed.status_code == 200
    assert failed.json() == {"ok": False, "error": "the endpoint answered HTTP 404"}
    history = client.get("/api/notifications/deliveries").json()
    assert history["total"] == 2
    newest, oldest = history["items"]
    assert (newest["status"], newest["is_test"]) == ("failed", True)
    assert newest["last_error"] == "the endpoint answered HTTP 404"
    assert oldest["status"] == "sent" and oldest["channel_name"] == "alerts"
    assert_no_secret(json.dumps(history))
    refreshed = client.get(CHANNELS).json()[0]
    assert refreshed["last_test_ok"] is False and refreshed["last_test_at"] is not None


def test_deleting_a_channel_removes_it(session_client: tuple[TestClient, str]) -> None:
    client, csrf = session_client
    channel = post(client, csrf, CHANNELS, slack_body()).json()
    deleted = client.delete(f"{CHANNELS}/{channel['id']}", headers=mutation_headers(client, csrf))
    assert deleted.status_code == 204
    assert client.get(CHANNELS).json() == []


# --- rules --------------------------------------------------------------------------------------


def test_rules_per_scope_with_inheritance_validation_and_audit(
    wired: AppContainer, session_client: tuple[TestClient, str]
) -> None:
    client, csrf = session_client
    channel = post(client, csrf, CHANNELS, slack_body()).json()["id"]
    headers = mutation_headers(client, csrf)
    assert client.get(RULES).json() == {"scope": "default", "rules": [], "inherited_from": None}

    default = {"rules": [{"channel_id": channel, "events": ["run.failed", "disk.usage_high"]}]}
    saved = client.put(RULES, json=default, headers=headers)
    assert saved.status_code == 200, saved.text
    assert saved.json()["rules"] == [
        {"channel_id": channel, "events": ["run.failed", "disk.usage_high"], "enabled": True}
    ]
    assert (
        client.get("/api/projects/demo/notifications/rules").json()["inherited_from"] == "default"
    )

    project_rules = {"rules": [{"channel_id": channel, "events": ["run.failed", "run.fixed"]}]}
    put = client.put("/api/projects/demo/notifications/rules", json=project_rules, headers=headers)
    assert put.json()["scope"] == "project" and put.json()["inherited_from"] is None
    assert client.get(DEV_RULES).json()["inherited_from"] == "project"

    muted = {"rules": [{"channel_id": channel, "events": [], "enabled": False}]}
    env_put = client.put(DEV_RULES, json=muted, headers=headers)
    assert (
        env_put.json()["scope"] == "environment" and env_put.json()["rules"][0]["enabled"] is False
    )

    # global-only events cannot be set below the default
    bad = {"rules": [{"channel_id": channel, "events": ["disk.usage_high"]}]}
    rejected = client.put("/api/projects/demo/notifications/rules", json=bad, headers=headers)
    assert rejected.status_code == 422 and "default rule" in rejected.json()["error"]["message"]
    unknown = {"rules": [{"channel_id": 999, "events": []}]}
    assert client.put(RULES, json=unknown, headers=headers).status_code == 404
    unknown_event = {"rules": [{"channel_id": channel, "events": ["nope"]}]}
    assert client.put(RULES, json=unknown_event, headers=headers).status_code == 422
    assert client.get("/api/projects/missing/notifications/rules").status_code == 404

    audited = [
        e
        for e in AuditService(wired.uow()).list(limit=50)
        if e.action == "notification.rules.update"
    ]
    assert [e.details["scope"] for e in audited] == ["environment", "project", "default"]
    assert audited[0].details["environment"] == "development"


def test_changing_rules_changes_where_events_are_queued(
    wired: AppContainer, session_client: tuple[TestClient, str]
) -> None:
    client, csrf = session_client
    channel = post(client, csrf, CHANNELS, slack_body()).json()["id"]
    headers = mutation_headers(client, csrf)
    emitter = OutboxEmitter(wired.uow, 30, wired.clock)
    assert emitter.emit(payload()) == 0  # no rules yet
    rules = {"rules": [{"channel_id": channel, "events": [NotificationEvent.RUN_FAILED.value]}]}
    client.put(RULES, json=rules, headers=headers)
    assert emitter.emit(payload(key="2")) == 1
    pending = client.get("/api/notifications/deliveries?status=pending").json()
    assert pending["total"] == 1 and pending["items"][0]["project"] == "demo"
    assert pending["items"][0]["next_attempt_at"] is not None


# --- history ------------------------------------------------------------------------------------


def test_delivery_history_filters_and_paging(
    wired: AppContainer, session_client: tuple[TestClient, str]
) -> None:
    client, csrf = session_client
    channel = post(client, csrf, CHANNELS, slack_body()).json()["id"]
    rules = {"rules": [{"channel_id": channel, "events": ["run.failed", "disk.usage_high"]}]}
    client.put(RULES, json=rules, headers=mutation_headers(client, csrf))
    emitter = OutboxEmitter(wired.uow, 0, wired.clock)
    for number in range(3):
        emitter.emit(payload(key=str(number)))
    emitter.emit(
        payload(NotificationEvent.DISK_USAGE_HIGH, key="data", project=None, environment=None)
    )
    everything = client.get("/api/notifications/deliveries").json()
    assert everything["total"] == 4 and everything["items"][0]["event"] == "disk.usage_high"
    assert client.get("/api/notifications/deliveries?project=demo").json()["total"] == 3
    page = client.get("/api/notifications/deliveries?limit=2&offset=3").json()
    assert page["total"] == 4 and len(page["items"]) == 1
    paged = client.get("/api/notifications/deliveries?per_page=3&page=2").json()
    assert (paged["total"], paged["pages"], len(paged["items"])) == (4, 2, 1)
    found = client.get("/api/notifications/deliveries", params={"q": "disk"}).json()
    assert found["total"] == 1 and found["items"][0]["event"] == "disk.usage_high"
    assert client.get("/api/notifications/deliveries?sort=bogus").status_code == 422
    assert (
        client.get(f"/api/notifications/deliveries?channel_id={channel}&status=sent").json()[
            "total"
        ]
        == 0
    )
    assert client.get("/api/notifications/deliveries?status=bogus").status_code == 422
    assert client.get("/api/notifications/deliveries?limit=0").status_code == 422
    assert client.get("/api/notifications/deliveries?project=missing").status_code == 404


# --- access -------------------------------------------------------------------------------------


def test_scopes_and_authentication(session_client: tuple[TestClient, str]) -> None:
    client, csrf = session_client
    reader = client.post(
        "/api/tokens",
        json={"name": "agent", "scopes": ["read"]},
        headers=mutation_headers(client, csrf),
    ).json()["token"]
    admin = client.post(
        "/api/tokens",
        json={"name": "admin", "scopes": ["admin"]},
        headers=mutation_headers(client, csrf),
    ).json()["token"]
    bare = TestClient(client.app, base_url=str(client.base_url))
    as_reader = {"Authorization": f"Bearer {reader}"}
    as_admin = {"Authorization": f"Bearer {admin}"}
    assert bare.get(CHANNELS).status_code == 401
    assert bare.get("/api/notifications/policy").status_code == 401
    assert bare.get(CHANNELS, headers=as_reader).status_code == 403
    assert bare.post(CHANNELS, json=slack_body(), headers=as_reader).status_code == 403
    assert bare.get(RULES, headers=as_reader).status_code == 403
    assert bare.get("/api/notifications/deliveries", headers=as_reader).status_code == 403
    assert bare.get("/api/notifications/policy", headers=as_reader).status_code == 200
    assert bare.post(CHANNELS, json=slack_body(), headers=as_admin).status_code == 201
    origin = {"Origin": "https://app.example.test"}
    assert client.post(CHANNELS, json=slack_body("two"), headers=origin).status_code == 403  # CSRF


def test_policy_lists_the_event_catalogue_and_the_operator_settings(
    session_client: tuple[TestClient, str],
) -> None:
    client, _csrf = session_client
    policy = client.get("/api/notifications/policy").json()
    assert policy["dedupe_minutes"] == 30 and policy["max_attempts"] == 7
    assert [e["event"] for e in policy["events"]] == [e.value for e in NotificationEvent]
    by_event = {e["event"]: e for e in policy["events"]}
    assert by_event["deployment.rolled_back"]["severity"] == "critical"
    assert by_event["disk.usage_high"]["project_scoped"] is False
    assert by_event["run.failed"]["project_scoped"] is True


def test_the_worker_being_down_is_a_503(tmp_path: Path, engine: Engine, clock: FakeClock) -> None:
    container = make_container(make_settings(tmp_path), engine, clock)
    create_user(container)
    client = make_client(container)
    csrf = login(client)
    assert post(client, csrf, CHANNELS, slack_body()).status_code == 503
