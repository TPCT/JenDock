"""Profile, password change, own sessions and own tokens (``/api/me``)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from backend.api.container import AppContainer
from backend.domain.audit import AuditFilter
from backend.services.audit_log import AuditLogService
from tests.api_support import (
    PASSWORD,
    USERNAME,
    FakeClock,
    create_user,
    login,
    make_client,
    mutation_headers,
)

FIREFOX = "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"
CHROME_MAC = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


@pytest.fixture
def csrf(client: TestClient, admin: int) -> str:
    return login(client)


def _post(client: TestClient, csrf: str, path: str, body: object | None = None) -> Response:
    return client.post(path, json=body, headers=mutation_headers(client, csrf))


def _login_as(container: AppContainer, agent: str) -> tuple[TestClient, str]:
    other = make_client(container)
    response = other.post(
        "/api/auth/login",
        json={"username": USERNAME, "password": PASSWORD},
        headers={"Origin": str(other.base_url).rstrip("/"), "User-Agent": agent},
    )
    assert response.status_code == 200, response.text
    return other, response.json()["csrf_token"]


def test_requires_a_session(client: TestClient, admin: int) -> None:
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me/sessions").status_code == 401


def test_profile_defaults_and_update(client: TestClient, csrf: str) -> None:
    body = client.get("/api/me").json()
    assert body["username"] == USERNAME
    assert body["display_name"] == ""
    assert body["email"] is None
    assert body["two_factor_enabled"] is False
    response = client.patch(
        "/api/me",
        json={"display_name": "  Ada Lovelace ", "email": "ada@example.com"},
        headers=mutation_headers(client, csrf),
    )
    assert response.status_code == 200
    assert response.json()["display_name"] == "Ada Lovelace"
    assert client.get("/api/me").json()["email"] == "ada@example.com"


def test_empty_values_clear_the_profile_fields(client: TestClient, csrf: str) -> None:
    headers = mutation_headers(client, csrf)
    client.patch("/api/me", json={"display_name": "Ada", "email": "a@b.co"}, headers=headers)
    cleared = client.patch("/api/me", json={"display_name": " ", "email": ""}, headers=headers)
    assert cleared.json()["display_name"] == ""
    assert cleared.json()["email"] is None


@pytest.mark.parametrize("email", ["nope", "a@b", "a b@c.de", "x" * 250 + "@a.bc"])
def test_invalid_email_is_rejected(client: TestClient, csrf: str, email: str) -> None:
    response = client.patch(
        "/api/me", json={"email": email}, headers=mutation_headers(client, csrf)
    )
    assert response.status_code == 422


def test_profile_update_is_audited_with_changed_fields(
    container: AppContainer, client: TestClient, csrf: str, admin: int
) -> None:
    client.patch("/api/me", json={"display_name": "Ada"}, headers=mutation_headers(client, csrf))
    page = AuditLogService(container.uow()).search(AuditFilter(action="user.profile_update"))
    assert [item.details["changed"] for item in page.items] == [["display_name"]]


def test_sessions_list_marks_current_and_summarizes_the_client(
    container: AppContainer, client: TestClient, csrf: str, clock: FakeClock
) -> None:
    clock.advance(minutes=5)
    _login_as(container, CHROME_MAC)
    rows = client.get("/api/me/sessions").json()
    assert len(rows) == 2
    assert [row["current"] for row in rows].count(True) == 1
    mac = next(row for row in rows if not row["current"])
    assert mac["client"] == "Chrome 126 on macOS"
    assert mac["user_agent"] == CHROME_MAC
    assert mac["ip_address"] == "testclient"
    assert rows[0]["last_seen_at"] >= rows[1]["last_seen_at"]
    assert "token" not in repr(rows).lower()


def test_revoke_one_session(container: AppContainer, client: TestClient, csrf: str) -> None:
    other, _ = _login_as(container, FIREFOX)
    rows = client.get("/api/me/sessions").json()
    target = next(row for row in rows if not row["current"])
    response = client.delete(
        f"/api/me/sessions/{target['id']}", headers=mutation_headers(client, csrf)
    )
    assert response.status_code == 204
    assert other.get("/api/auth/session").status_code == 401
    assert len(client.get("/api/me/sessions").json()) == 1


def test_cannot_revoke_the_current_session_or_a_stranger_s(
    container: AppContainer, client: TestClient, csrf: str
) -> None:
    current = next(r for r in client.get("/api/me/sessions").json() if r["current"])
    headers = mutation_headers(client, csrf)
    assert client.delete(f"/api/me/sessions/{current['id']}", headers=headers).status_code == 422
    create_user(container, "grace", "grace hopper rocks")
    stranger = make_client(container)
    login(stranger, "grace", "grace hopper rocks")
    theirs = stranger.get("/api/me/sessions").json()[0]["id"]
    assert client.delete(f"/api/me/sessions/{theirs}", headers=headers).status_code == 404
    assert stranger.get("/api/auth/session").status_code == 200


def test_revoke_all_other_sessions(container: AppContainer, client: TestClient, csrf: str) -> None:
    others = [_login_as(container, FIREFOX)[0] for _ in range(2)]
    response = _post(client, csrf, "/api/me/sessions/revoke-others")
    assert response.json() == {"sessions_ended": 2}
    assert all(other.get("/api/auth/session").status_code == 401 for other in others)
    assert len(client.get("/api/me/sessions").json()) == 1


def test_expired_sessions_are_not_listed(
    container: AppContainer, client: TestClient, csrf: str, clock: FakeClock
) -> None:
    _login_as(container, FIREFOX)
    clock.advance(minutes=30)
    client.get("/api/auth/session")  # keeps this session alive
    clock.advance(minutes=40)  # the other one is now idle for over an hour
    rows = client.get("/api/me/sessions").json()
    assert [row["current"] for row in rows] == [True]


def test_security_log_shows_only_own_activity(
    container: AppContainer, client: TestClient, csrf: str
) -> None:
    create_user(container, "grace", "grace hopper rocks")
    grace = make_client(container)
    grace_csrf = login(grace, "grace", "grace hopper rocks")
    grace.patch(
        "/api/me", json={"display_name": "Grace"}, headers=mutation_headers(grace, grace_csrf)
    )
    client.patch("/api/me", json={"display_name": "Ada"}, headers=mutation_headers(client, csrf))
    page = client.get("/api/me/security-log").json()
    actions = [item["action"] for item in page["items"]]
    assert "user.profile_update" in actions
    assert "auth.login" in actions
    assert {item["target_id"] for item in page["items"] if item["target_type"] == "user"} <= {
        str(client.get("/api/me").json()["id"])
    }
    assert all("Grace" not in repr(item) for item in page["items"])
    assert page["items"][0]["description"]
