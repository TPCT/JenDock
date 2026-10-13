from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from argon2 import PasswordHasher as Argon2Hasher
from fastapi import Depends
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine, select

from backend.api.app import create_app
from backend.api.container import AppContainer
from backend.api.deps import require_scope
from backend.api.principal import Principal
from backend.config import SESSION_ABSOLUTE_TIMEOUT, SESSION_IDLE_TIMEOUT
from backend.domain.enums import TokenScope
from backend.domain.errors import RateLimitedError
from backend.repositories.models import Session, User
from backend.services.audit import AuditService
from backend.services.passwords import PasswordHasher
from backend.services.rate_limit import LoginRateLimiter
from tests.api_support import (
    DEV_ORIGIN,
    ORIGIN,
    PASSWORD,
    USERNAME,
    FakeClock,
    create_user,
    fast_hasher,
    login,
    make_client,
    make_container,
    make_settings,
    mutation_headers,
)

LOGIN = "/api/auth/login"
ORIGIN_HEADER = {"Origin": ORIGIN}


def _post_login(client: TestClient, username: str, password: str) -> Response:
    body = {"username": username, "password": password}
    response: Response = client.post(LOGIN, json=body, headers=ORIGIN_HEADER)
    return response


def _session_rows(container: AppContainer) -> list[str]:
    uow = container.uow()
    with uow:
        return list(uow.session.scalars(select(Session.token_hash)).all())


def test_login_success_returns_user_and_csrf(client: TestClient, admin: int) -> None:
    response = client.post(
        LOGIN, json={"username": USERNAME, "password": PASSWORD}, headers=ORIGIN_HEADER
    )
    assert response.status_code == 200
    body = response.json()
    assert body["user"] == {"id": admin, "username": USERNAME}
    assert len(body["csrf_token"]) == 64
    session = client.get("/api/auth/session")
    assert session.status_code == 200
    assert session.json()["csrf_token"] == body["csrf_token"]
    assert "password" not in response.text


def test_session_requires_login(client: TestClient) -> None:
    assert client.get("/api/auth/session").status_code == 401


def test_wrong_password_and_unknown_user_are_indistinguishable(
    client: TestClient, admin: int
) -> None:
    wrong = client.post(
        LOGIN, json={"username": USERNAME, "password": "wrong password!"}, headers=ORIGIN_HEADER
    )
    unknown = client.post(
        LOGIN, json={"username": "ghost", "password": "wrong password!"}, headers=ORIGIN_HEADER
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert "set-cookie" not in wrong.headers


def test_unknown_user_still_runs_a_password_verification(
    container: AppContainer, client: TestClient, admin: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    original = container.hasher.verify_dummy

    def spy(password: str) -> None:
        calls.append(password)
        original(password)

    monkeypatch.setattr(container.hasher, "verify_dummy", spy)
    _post_login(client, "ghost", "wrong password!")
    assert calls == ["wrong password!"]
    _post_login(client, USERNAME, "wrong password!")
    assert calls == ["wrong password!"]


def test_login_requires_exact_origin(client: TestClient, admin: int) -> None:
    body = {"username": USERNAME, "password": PASSWORD}
    assert client.post(LOGIN, json=body).status_code == 403
    assert (
        client.post(LOGIN, json=body, headers={"Origin": "https://evil.example"}).status_code == 403
    )
    assert (
        client.post(LOGIN, json=body, headers={"Origin": ORIGIN + ".evil.example"}).status_code
        == 403
    )


def test_failed_login_for_known_user_is_audited_without_password(
    container: AppContainer, client: TestClient, admin: int
) -> None:
    _post_login(client, USERNAME, "wrong password!")
    events = AuditService(container.uow()).list(action="auth.login_failed")
    assert len(events) == 1
    assert "wrong password" not in repr(events[0].details)


def test_successful_login_is_audited(
    container: AppContainer, client: TestClient, admin: int
) -> None:
    login(client)
    events = AuditService(container.uow()).list(action="auth.login")
    assert [e.actor_id for e in events] == [str(admin)]


def test_rehash_on_login(container: AppContainer) -> None:
    weak = PasswordHasher(Argon2Hasher(time_cost=1, memory_cost=8, parallelism=1))
    strong = PasswordHasher(Argon2Hasher(time_cost=2, memory_cost=16, parallelism=1))
    old_hash = weak.hash(PASSWORD)
    uow = container.uow()
    with uow:
        uow.users.add(User(username=USERNAME, password_hash=old_hash))
    client = make_client(replace(container, hasher=strong))
    login(client)
    with uow:
        user = uow.users.get_by_username(USERNAME)
        assert user is not None
        assert user.password_hash != old_hash
        assert not strong.needs_rehash(user.password_hash)
        assert strong.verify(user.password_hash, PASSWORD)


def test_username_rate_limit_returns_429_with_retry_after(client: TestClient, admin: int) -> None:
    for _ in range(5):
        assert _post_login(client, USERNAME, "wrong password!").status_code == 401
    blocked = client.post(
        LOGIN, json={"username": USERNAME, "password": PASSWORD}, headers=ORIGIN_HEADER
    )
    assert blocked.status_code == 429
    assert 0 < int(blocked.headers["Retry-After"]) <= 900
    assert blocked.json()["error"]["code"] == "rate_limited"


def test_username_limit_expires_after_window(
    client: TestClient, clock: FakeClock, admin: int
) -> None:
    for _ in range(5):
        _post_login(client, USERNAME, "wrong password!")
    clock.advance(minutes=15, seconds=1)
    login(client)


def test_ip_rate_limit_applies_across_usernames(client: TestClient, admin: int) -> None:
    for index in range(20):
        assert _post_login(client, f"user{index}", "wrong password!").status_code == 401
    blocked = client.post(
        LOGIN, json={"username": "another", "password": "x"}, headers=ORIGIN_HEADER
    )
    assert blocked.status_code == 429
    assert "Retry-After" in blocked.headers


def test_successful_login_resets_username_counter(client: TestClient, admin: int) -> None:
    for _ in range(4):
        _post_login(client, USERNAME, "wrong password!")
    login(client)
    for _ in range(4):
        assert _post_login(client, USERNAME, "wrong password!").status_code == 401


def test_limiter_unit_boundaries() -> None:
    clock = FakeClock()
    limiter = LoginRateLimiter(clock, max_per_username=2, max_per_ip=10)
    limiter.record_failure("a", "1.1.1.1")
    limiter.check("a", "1.1.1.1")
    limiter.record_failure("A", "1.1.1.1")
    with pytest.raises(RateLimitedError) as error:
        limiter.check("a", "9.9.9.9")
    assert error.value.retry_after == 900
    clock.advance(minutes=10)
    with pytest.raises(RateLimitedError) as later:
        limiter.check("a", "9.9.9.9")
    assert later.value.retry_after == 300


def test_idle_expiry_and_lazy_delete(
    container: AppContainer, client: TestClient, clock: FakeClock, admin: int, engine: Engine
) -> None:
    login(client)
    clock.advance(minutes=59)
    assert client.get("/api/auth/session").status_code == 200
    clock.advance(minutes=59)
    assert client.get("/api/auth/session").status_code == 200
    clock.advance(seconds=SESSION_IDLE_TIMEOUT.total_seconds() + 1)
    assert client.get("/api/auth/session").status_code == 401
    assert _session_rows(container) == []


def test_absolute_expiry_even_when_active(client: TestClient, clock: FakeClock, admin: int) -> None:
    login(client)
    for _ in range(12):
        clock.advance(minutes=59)
        assert client.get("/api/auth/session").status_code == 200
    assert timedelta(minutes=59 * 12) < SESSION_ABSOLUTE_TIMEOUT
    clock.advance(minutes=59)
    assert client.get("/api/auth/session").status_code == 401


def _client_with_background_route(container: AppContainer) -> TestClient:
    app = create_app(container=container)

    def background(
        principal: Principal = Depends(require_scope(TokenScope.READ, background=True)),  # noqa: B008
    ) -> dict[str, bool]:
        return {"session": principal.is_session}

    app.add_api_route("/bg", background)
    return TestClient(app, base_url=ORIGIN)


def test_background_request_does_not_extend_idle_expiry(
    container: AppContainer, clock: FakeClock, admin: int
) -> None:
    client = _client_with_background_route(container)
    login(client)
    clock.advance(minutes=40)
    assert client.get("/bg").status_code == 200
    clock.advance(minutes=30)
    assert client.get("/bg").status_code == 401


def test_normal_request_extends_idle_expiry(
    container: AppContainer, clock: FakeClock, admin: int
) -> None:
    client = _client_with_background_route(container)
    login(client)
    clock.advance(minutes=40)
    assert client.get("/api/auth/session").status_code == 200
    clock.advance(minutes=40)
    assert client.get("/bg").status_code == 200


def test_production_cookie_attributes(client: TestClient, admin: int) -> None:
    response = client.post(
        LOGIN, json={"username": USERNAME, "password": PASSWORD}, headers=ORIGIN_HEADER
    )
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("__Host-session=")
    lowered = cookie.lower()
    for attribute in ("secure", "httponly", "samesite=strict", "path=/"):
        assert attribute in lowered
    assert "domain" not in lowered


def test_development_http_cookie_attributes(
    tmp_path: Path, engine: Engine, clock: FakeClock
) -> None:
    container = make_container(make_settings(tmp_path, origin=DEV_ORIGIN), engine, clock)
    create_user(container)
    client = make_client(container)
    response = client.post(
        LOGIN,
        json={"username": USERNAME, "password": PASSWORD},
        headers={"Origin": DEV_ORIGIN},
    )
    assert response.status_code == 200
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("jendock_session=")
    lowered = cookie.lower()
    assert "secure" not in lowered
    assert "httponly" in lowered
    assert "samesite=strict" in lowered
    assert client.get("/api/auth/session").status_code == 200


def test_session_token_is_stored_only_as_hash(
    container: AppContainer, client: TestClient, admin: int
) -> None:
    login(client)
    raw = client.cookies["__Host-session"]
    assert len(raw) >= 43
    stored = _session_rows(container)
    assert raw not in stored
    assert len(stored) == 1
    assert len(stored[0]) == 64


def test_csrf_missing_wrong_and_correct(client: TestClient, admin: int) -> None:
    csrf = login(client)
    url = "/api/auth/logout"
    assert client.post(url, headers=ORIGIN_HEADER).status_code == 403
    assert client.post(url, headers={**ORIGIN_HEADER, "X-CSRF-Token": "wrong"}).status_code == 403
    assert (
        client.post(url, headers={**ORIGIN_HEADER, "X-CSRF-Token": csrf[::-1]}).status_code == 403
    )
    assert client.get("/api/auth/session").status_code == 200
    assert client.post(url, headers=mutation_headers(client, csrf)).status_code == 204


def test_wrong_or_missing_origin_rejected_even_with_valid_csrf(
    client: TestClient, admin: int
) -> None:
    csrf = login(client)
    url = "/api/auth/logout"
    assert client.post(url, headers={"X-CSRF-Token": csrf}).status_code == 403
    evil = {"Origin": "https://evil.example", "X-CSRF-Token": csrf}
    assert client.post(url, headers=evil).status_code == 403
    assert client.get("/api/auth/session").status_code == 200


def test_csrf_token_is_bound_to_its_session(
    client: TestClient, container: AppContainer, admin: int
) -> None:
    first = login(client)
    other = make_client(container)
    second = login(other)
    assert first != second
    response = other.post("/api/auth/logout", headers={**ORIGIN_HEADER, "X-CSRF-Token": first})
    assert response.status_code == 403


def test_logout_deletes_session_and_clears_cookie(
    container: AppContainer, client: TestClient, admin: int
) -> None:
    csrf = login(client)
    raw = client.cookies["__Host-session"]
    response = client.post("/api/auth/logout", headers=mutation_headers(client, csrf))
    assert response.status_code == 204
    assert "__Host-session=" in response.headers["set-cookie"]
    assert _session_rows(container) == []
    client.cookies.set("__Host-session", raw)
    assert client.get("/api/auth/session").status_code == 401
    events = AuditService(container.uow()).list(action="auth.logout")
    assert len(events) == 1


def test_inactive_user_cannot_login(
    container: AppContainer, client: TestClient, admin: int
) -> None:
    with container.uow() as uow:
        user = uow.users.get(admin)
        assert user is not None
        user.is_active = False
    assert _post_login(client, USERNAME, PASSWORD).status_code == 401


def test_validation_errors_do_not_echo_input(client: TestClient) -> None:
    response = client.post(
        LOGIN, json={"username": "x", "password": "p", "extra": "leak-me"}, headers=ORIGIN_HEADER
    )
    assert response.status_code == 422
    assert "leak-me" not in response.text


def test_hasher_fixture_is_fast() -> None:
    assert fast_hasher().hash("x" * 12)
