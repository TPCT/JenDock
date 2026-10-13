"""Two-factor authentication: enrollment, login second step, recovery codes, disabling."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import select

from backend.api.container import AppContainer
from backend.domain.audit import AuditFilter
from backend.repositories.models import RecoveryCode, User
from backend.services.audit_log import AuditLogService
from backend.services.totp import decode_key, hotp, time_step
from tests.api_support import (
    PASSWORD,
    USERNAME,
    FakeClock,
    login,
    make_client,
    mutation_headers,
)


def _code(secret: str, clock: FakeClock, offset: int = 0) -> str:
    return hotp(decode_key(secret), time_step(clock.timestamp()) + offset)


def _post(client: TestClient, csrf: str, path: str, body: object | None = None) -> Response:
    return client.post(path, json=body, headers=mutation_headers(client, csrf))


@pytest.fixture
def csrf(client: TestClient, admin: int) -> str:
    return login(client)


def _enroll(client: TestClient, csrf: str, clock: FakeClock) -> tuple[str, list[str]]:
    """Enable two-factor; returns the key text and the recovery codes."""
    enrollment = _post(client, csrf, "/api/me/two-factor/enroll")
    assert enrollment.status_code == 200, enrollment.text
    secret = enrollment.json()["secret"]
    enabled = _post(client, csrf, "/api/me/two-factor/enable", {"code": _code(secret, clock)})
    assert enabled.status_code == 200, enabled.text
    codes = enabled.json()["recovery_codes"]
    clock.advance(seconds=30)  # the enrollment code's step is used up
    return secret, codes


def _password_login(container: AppContainer) -> tuple[TestClient, Response]:
    fresh = make_client(container)
    response = fresh.post(
        "/api/auth/login",
        json={"username": USERNAME, "password": PASSWORD},
        headers={"Origin": str(fresh.base_url).rstrip("/")},
    )
    return fresh, response


def _second_step(fresh: TestClient, challenge: str, code: str) -> Response:
    return fresh.post(
        "/api/auth/login/two-factor",
        json={"challenge": challenge, "code": code},
        headers={"Origin": str(fresh.base_url).rstrip("/")},
    )


def test_status_starts_off(client: TestClient, csrf: str) -> None:
    assert client.get("/api/me/two-factor").json() == {
        "enabled": False,
        "enabled_at": None,
        "recovery_codes_left": 0,
    }


def test_enroll_returns_key_and_provisioning_uri(client: TestClient, csrf: str) -> None:
    body = _post(client, csrf, "/api/me/two-factor/enroll").json()
    assert len(body["secret"]) == 32
    assert body["uri"].startswith("otpauth://totp/Jendock%3Aadmin?")
    assert f"secret={body['secret']}" in body["uri"]
    # Pending until verified: two-factor is still off.
    assert client.get("/api/me/two-factor").json()["enabled"] is False


def test_enable_with_the_right_code_returns_ten_recovery_codes(
    client: TestClient, csrf: str, clock: FakeClock
) -> None:
    _secret, codes = _enroll(client, csrf, clock)
    assert len(codes) == 10
    assert len(set(codes)) == 10
    assert all(len(code) == 19 and code.count("-") == 3 for code in codes)
    status = client.get("/api/me/two-factor").json()
    assert status["enabled"] is True
    assert status["recovery_codes_left"] == 10
    assert client.get("/api/me").json()["two_factor_enabled"] is True


def test_enable_rejects_a_wrong_code_and_needs_enrollment(client: TestClient, csrf: str) -> None:
    assert _post(client, csrf, "/api/me/two-factor/enable", {"code": "123456"}).status_code == 422
    _post(client, csrf, "/api/me/two-factor/enroll")
    wrong = _post(client, csrf, "/api/me/two-factor/enable", {"code": "000000"})
    assert wrong.status_code == 422
    assert client.get("/api/me/two-factor").json()["enabled"] is False


def test_secret_and_recovery_codes_are_not_stored_in_the_clear(
    container: AppContainer, client: TestClient, csrf: str, clock: FakeClock
) -> None:
    enrollment = _post(client, csrf, "/api/me/two-factor/enroll").json()
    secret = enrollment["secret"]
    enabled = _post(client, csrf, "/api/me/two-factor/enable", {"code": _code(secret, clock)})
    codes = enabled.json()["recovery_codes"]
    uow = container.uow()
    with uow:
        user = uow.session.scalars(select(User)).one()
        assert user.totp_secret is not None
        assert secret.encode() not in user.totp_secret
        hashes = uow.session.scalars(select(RecoveryCode.code_hash)).all()
    assert len(hashes) == 10
    for code in codes:
        assert code not in hashes
        assert code.replace("-", "") not in hashes
    log = AuditLogService(container.uow()).search(AuditFilter())
    text = repr([item.details for item in log.items])
    assert secret not in text
    assert all(code not in text for code in codes)


def test_enroll_when_already_enabled_conflicts(
    client: TestClient, csrf: str, clock: FakeClock
) -> None:
    _enroll(client, csrf, clock)
    assert _post(client, csrf, "/api/me/two-factor/enroll").status_code == 409
    assert _post(client, csrf, "/api/me/two-factor/enable", {"code": "000000"}).status_code == 409


def test_login_asks_for_the_code_and_opens_a_session_after_it(
    container: AppContainer, client: TestClient, csrf: str, clock: FakeClock
) -> None:
    secret, _codes = _enroll(client, csrf, clock)
    fresh, first = _password_login(container)
    assert first.status_code == 200
    assert first.json()["mfa_required"] is True
    assert "csrf_token" not in first.json()
    assert fresh.get("/api/auth/session").status_code == 401  # no session yet
    second = _second_step(fresh, first.json()["challenge"], _code(secret, clock))
    assert second.status_code == 200, second.text
    assert second.json()["user"]["username"] == USERNAME
    assert fresh.get("/api/auth/session").status_code == 200


def test_login_second_step_rejects_a_wrong_code_and_keeps_no_session(
    container: AppContainer, client: TestClient, csrf: str, clock: FakeClock
) -> None:
    secret, _ = _enroll(client, csrf, clock)
    fresh, first = _password_login(container)
    challenge = first.json()["challenge"]
    bad = _second_step(fresh, challenge, "000000")
    assert bad.status_code == 401
    assert fresh.get("/api/auth/session").status_code == 401
    good = _second_step(fresh, challenge, _code(secret, clock))
    assert good.status_code == 200  # the challenge survives a few mistakes


def test_a_code_cannot_be_replayed(
    container: AppContainer, client: TestClient, csrf: str, clock: FakeClock
) -> None:
    secret, _ = _enroll(client, csrf, clock)
    code = _code(secret, clock)
    fresh, first = _password_login(container)
    assert _second_step(fresh, first.json()["challenge"], code).status_code == 200
    other, again = _password_login(container)
    assert _second_step(other, again.json()["challenge"], code).status_code == 401
    clock.advance(seconds=30)
    assert _second_step(other, again.json()["challenge"], _code(secret, clock)).status_code == 200


def test_challenge_is_single_use_and_expires(
    container: AppContainer, client: TestClient, csrf: str, clock: FakeClock
) -> None:
    secret, _ = _enroll(client, csrf, clock)
    fresh, first = _password_login(container)
    challenge = first.json()["challenge"]
    assert _second_step(fresh, challenge, _code(secret, clock)).status_code == 200
    clock.advance(seconds=30)
    assert _second_step(fresh, challenge, _code(secret, clock)).status_code == 401
    expired, second = _password_login(container)
    clock.advance(minutes=6)
    assert (
        _second_step(expired, second.json()["challenge"], _code(secret, clock)).status_code == 401
    )


def test_unknown_challenge_is_rejected(container: AppContainer, client: TestClient) -> None:
    fresh = make_client(container)
    assert _second_step(fresh, "nope", "123456").status_code == 401


def test_second_step_failures_are_rate_limited_like_login(
    container: AppContainer, client: TestClient, csrf: str, clock: FakeClock
) -> None:
    secret, _ = _enroll(client, csrf, clock)
    fresh, first = _password_login(container)
    challenge = first.json()["challenge"]
    statuses = [_second_step(fresh, challenge, "000000").status_code for _ in range(5)]
    assert statuses == [401] * 5
    # Five failures also burn the challenge, and the username is locked out like for passwords.
    blocked = _second_step(fresh, challenge, _code(secret, clock))
    assert blocked.status_code in {401, 429}
    _again, retry = _password_login(container)
    assert retry.status_code == 429


def test_recovery_code_logs_in_once(
    container: AppContainer, client: TestClient, csrf: str, clock: FakeClock
) -> None:
    _secret, codes = _enroll(client, csrf, clock)
    fresh, first = _password_login(container)
    ok = _second_step(fresh, first.json()["challenge"], codes[0].upper())
    assert ok.status_code == 200
    again, second = _password_login(container)
    assert _second_step(again, second.json()["challenge"], codes[0]).status_code == 401
    assert client.get("/api/me/two-factor").json()["recovery_codes_left"] == 9
    page = AuditLogService(container.uow()).search(AuditFilter(action="auth.recovery_code_used"))
    assert len(page.items) == 1


def test_regenerating_recovery_codes_replaces_the_old_ones(
    container: AppContainer, client: TestClient, csrf: str, clock: FakeClock
) -> None:
    secret, old = _enroll(client, csrf, clock)
    fresh = _post(client, csrf, "/api/me/two-factor/recovery-codes", {"code": _code(secret, clock)})
    assert fresh.status_code == 200
    new = fresh.json()["recovery_codes"]
    assert len(new) == 10
    assert not set(new) & set(old)
    other, first = _password_login(container)
    assert _second_step(other, first.json()["challenge"], old[0]).status_code == 401


def test_disable_needs_password_and_code(
    container: AppContainer, client: TestClient, csrf: str, clock: FakeClock
) -> None:
    secret, _ = _enroll(client, csrf, clock)
    code = _code(secret, clock)
    no_password = _post(
        client, csrf, "/api/me/two-factor/disable", {"password": "wrong", "code": code}
    )
    assert no_password.status_code == 422
    no_code = _post(
        client, csrf, "/api/me/two-factor/disable", {"password": PASSWORD, "code": "000000"}
    )
    assert no_code.status_code == 422
    assert client.get("/api/me/two-factor").json()["enabled"] is True
    done = _post(client, csrf, "/api/me/two-factor/disable", {"password": PASSWORD, "code": code})
    assert done.status_code == 204
    assert client.get("/api/me/two-factor").json() == {
        "enabled": False,
        "enabled_at": None,
        "recovery_codes_left": 0,
    }
    _, plain = _password_login(container)
    assert "mfa_required" not in plain.json()  # back to a plain password login


def test_disable_with_a_recovery_code(client: TestClient, csrf: str, clock: FakeClock) -> None:
    _secret, codes = _enroll(client, csrf, clock)
    done = _post(
        client, csrf, "/api/me/two-factor/disable", {"password": PASSWORD, "code": codes[3]}
    )
    assert done.status_code == 204


def test_disable_when_off_conflicts(client: TestClient, csrf: str) -> None:
    response = _post(
        client, csrf, "/api/me/two-factor/disable", {"password": PASSWORD, "code": "123456"}
    )
    assert response.status_code == 409


def test_lifecycle_is_audited(
    container: AppContainer, client: TestClient, csrf: str, clock: FakeClock
) -> None:
    secret, _ = _enroll(client, csrf, clock)
    _post(
        client,
        csrf,
        "/api/me/two-factor/disable",
        {"password": PASSWORD, "code": _code(secret, clock)},
    )
    page = AuditLogService(container.uow()).search(AuditFilter(action="two_factor.*"))
    assert sorted(item.action for item in page.items) == ["two_factor.disable", "two_factor.enable"]
