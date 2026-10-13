"""User management: create, edit, disable, delete, reset, forced password change, audit."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from backend.api.container import AppContainer
from backend.domain.access import AccessRole, GlobalRole
from backend.domain.enums import TokenScope
from backend.domain.errors import ConflictError
from backend.services.audit import AuditService
from backend.services.users import UserChange
from tests.access_support import SYSTEM, Session, add_user, grant, sign_in
from tests.api_support import ORIGIN, PASSWORD, login, make_client

NEW_PASSWORD = "a brand new passphrase"


@pytest.fixture
def owner(container: AppContainer, admin: int) -> Session:
    """The default ``admin`` test user is an owner."""
    return sign_in(container, "admin")


def _actions(container: AppContainer, action: str) -> list[dict[str, object]]:
    return [dict(e.details) for e in AuditService(container.uow()).list(action=action)]


def _json(response: Response) -> Any:  # Any: decoded JSON
    return response.json()


def _status(response: Response) -> int:
    return int(response.status_code)


def _create(session: Session, username: str, role: str = "member", **extra: object) -> Response:
    return session.post("/api/users", {"username": username, "role": role, **extra})


def test_create_returns_a_one_time_password_once(container: AppContainer, owner: Session) -> None:
    response = _create(owner, "alice", "member", display_name="Alice A", email="alice@example.org")
    assert _status(response) == 201
    body = _json(response)
    assert body["user"]["username"] == "alice"
    assert body["user"]["role"] == "member"
    assert body["user"]["must_change_password"] is True
    password = body["one_time_password"]
    assert len(password) >= 12
    assert "password_hash" not in repr(body)
    listing = _json(owner.get("/api/users"))
    assert [u["username"] for u in listing] == ["admin", "alice"]
    assert password not in repr(listing)
    assert password not in repr(_actions(container, "user.create"))
    assert _actions(container, "user.create")[0]["role"] == "member"


def test_duplicate_and_invalid_usernames_are_rejected(owner: Session) -> None:
    assert _status(_create(owner, "alice")) == 201
    assert _status(_create(owner, "alice")) == 409
    assert _status(_create(owner, "Bad Name!")) == 422
    assert _status(_create(owner, "bob", email="not-an-email")) == 422


def test_members_and_anonymous_cannot_manage_users(container: AppContainer, owner: Session) -> None:
    add_user(container, "mia")
    mia = sign_in(container, "mia")
    assert _status(mia.get("/api/users")) == 403
    assert _status(_create(mia, "x1x1x1")) == 403
    assert _status(make_client(container).get("/api/users")) == 401


def test_a_token_cannot_manage_users_even_for_an_owner(
    container: AppContainer, owner: Session
) -> None:
    created = container.token_service().create("t", [TokenScope.ADMIN], owner.user_id, None, SYSTEM)
    response = TestClient(owner.client.app, base_url=ORIGIN).get(
        "/api/users", headers={"Authorization": f"Bearer {created.value}"}
    )
    assert response.status_code == 403


def test_forced_password_change_flow(container: AppContainer, owner: Session) -> None:
    password = _json(_create(owner, "alice"))["one_time_password"]
    alice = sign_in(container, "alice", password)
    session = _json(alice.get("/api/auth/session"))
    assert session["must_change_password"] is True
    blocked = alice.get("/api/projects")
    assert _status(blocked) == 403
    assert _json(blocked)["error"]["code"] == "password_change_required"
    assert _status(alice.get("/api/runs")) == 403
    assert _status(alice.get("/api/tokens")) == 403
    # wrong current password and weak new passwords are refused
    path = "/api/auth/change-password"
    assert (
        _status(alice.post(path, {"current_password": "nope" * 5, "new_password": NEW_PASSWORD}))
        == 422
    )
    assert _status(alice.post(path, {"current_password": password, "new_password": "short"})) == 422
    assert (
        _status(alice.post(path, {"current_password": password, "new_password": password})) == 422
    )
    ok = alice.post(path, {"current_password": password, "new_password": NEW_PASSWORD})
    assert _status(ok) == 200
    assert _json(ok)["must_change_password"] is False
    assert _status(alice.get("/api/projects")) == 200
    assert _actions(container, "user.password_change")
    assert NEW_PASSWORD not in repr(_actions(container, "user.password_change"))
    # the one-time password no longer works, the new one does
    again = make_client(container)
    assert (
        again.post(
            "/api/auth/login",
            json={"username": "alice", "password": password},
            headers={"Origin": ORIGIN},
        ).status_code
        == 401
    )
    assert login(make_client(container), "alice", NEW_PASSWORD)


def test_logout_is_possible_while_a_change_is_pending(
    container: AppContainer, owner: Session
) -> None:
    password = _json(_create(owner, "alice"))["one_time_password"]
    alice = sign_in(container, "alice", password)
    assert _status(alice.post("/api/auth/logout")) == 204


def test_changing_the_password_ends_other_sessions(container: AppContainer, owner: Session) -> None:
    other = sign_in(container, "admin")
    assert _status(other.get("/api/projects")) == 200
    body = {"current_password": PASSWORD, "new_password": NEW_PASSWORD}
    assert _status(owner.post("/api/auth/change-password", body)) == 200
    assert _status(other.get("/api/projects")) == 401
    assert _status(owner.get("/api/projects")) == 200


def test_admin_reset_password_is_one_time_and_forces_a_change(
    container: AppContainer, owner: Session
) -> None:
    add_user(container, "mia")
    mia = sign_in(container, "mia")
    mia_id = mia.user_id
    response = owner.post(f"/api/users/{mia_id}/reset-password")
    assert _status(response) == 200
    one_time = _json(response)["one_time_password"]
    assert len(one_time) >= 12
    assert _status(mia.get("/api/projects")) == 401  # her session ended
    old = make_client(container).post(
        "/api/auth/login",
        json={"username": "mia", "password": PASSWORD},
        headers={"Origin": ORIGIN},
    )
    assert old.status_code == 401
    fresh = sign_in(container, "mia", one_time)
    assert _json(fresh.get("/api/auth/session"))["must_change_password"] is True
    audit = _actions(container, "user.password_reset")
    assert audit and one_time not in repr(audit) and audit[-1]["username"] == "mia"
    # a later reset gives a different password
    assert _json(owner.post(f"/api/users/{mia_id}/reset-password"))["one_time_password"] != one_time


def test_admin_cannot_manage_owners(container: AppContainer, owner: Session) -> None:
    add_user(container, "ada", GlobalRole.ADMIN)
    ada = sign_in(container, "ada")
    assert _status(_create(ada, "newowner", "owner")) == 403
    assert _status(_create(ada, "newadmin", "admin")) == 201
    assert _status(_create(ada, "newmember", "member")) == 201
    assert _status(ada.patch(f"/api/users/{owner.user_id}", {"display_name": "x"})) == 403
    assert _status(ada.post(f"/api/users/{owner.user_id}/reset-password")) == 403
    assert _status(ada.delete(f"/api/users/{owner.user_id}")) == 403
    assert _status(ada.patch(f"/api/users/{owner.user_id}", {"is_active": False})) == 403
    member = _json(_create(owner, "mem"))["user"]["id"]
    assert _status(ada.patch(f"/api/users/{member}", {"role": "owner"})) == 403
    assert _status(ada.patch(f"/api/users/{member}", {"role": "admin"})) == 200
    assert _status(ada.delete(f"/api/users/{member}")) == 204


def test_owner_can_manage_owners_and_admins(container: AppContainer, owner: Session) -> None:
    second = _json(_create(owner, "second", "owner"))["user"]["id"]
    assert _status(owner.patch(f"/api/users/{second}", {"role": "admin"})) == 200
    assert _status(owner.patch(f"/api/users/{second}", {"role": "owner"})) == 200
    assert _status(owner.delete(f"/api/users/{second}")) == 204


def test_nobody_edits_their_own_role_or_removes_themselves(
    container: AppContainer, owner: Session
) -> None:
    _create(owner, "second", "owner")
    me = owner.user_id
    assert _status(owner.patch(f"/api/users/{me}", {"role": "member"})) == 409
    assert _status(owner.patch(f"/api/users/{me}", {"is_active": False})) == 409
    assert _status(owner.delete(f"/api/users/{me}")) == 409
    assert _status(owner.patch(f"/api/users/{me}", {"display_name": "Me"})) == 200


def test_last_owner_is_protected(container: AppContainer, owner: Session) -> None:
    service = container.user_service()
    last = owner.user_id
    for change in (UserChange(role=GlobalRole.ADMIN), UserChange(is_active=False)):
        with pytest.raises(ConflictError, match="last owner"):
            service.update(last, change, SYSTEM)
    with pytest.raises(ConflictError, match="last owner"):
        service.delete(last, SYSTEM)
    other = add_user(container, "second", GlobalRole.OWNER)
    service.update(other, UserChange(role=GlobalRole.ADMIN), SYSTEM)  # still one owner left
    with pytest.raises(ConflictError, match="last owner"):
        service.update(last, UserChange(role=GlobalRole.MEMBER), SYSTEM)
    # a disabled owner does not count: the remaining one stays protected
    service.update(other, UserChange(role=GlobalRole.OWNER), SYSTEM)
    service.update(other, UserChange(is_active=False), SYSTEM)
    with pytest.raises(ConflictError, match="last owner"):
        service.delete(last, SYSTEM)


def test_disabled_users_lose_their_sessions_tokens_and_login(
    container: AppContainer, owner: Session
) -> None:
    mia_id = add_user(container, "mia")
    mia = sign_in(container, "mia")
    created = container.token_service().create("t", [TokenScope.READ], mia_id, None, SYSTEM)
    bearer = {"Authorization": f"Bearer {created.value}"}
    bare = TestClient(owner.client.app, base_url=ORIGIN)
    assert bare.get("/api/projects", headers=bearer).status_code == 200
    assert _status(owner.patch(f"/api/users/{mia_id}", {"is_active": False})) == 200
    assert _status(mia.get("/api/projects")) == 401
    assert bare.get("/api/projects", headers=bearer).status_code == 401
    refused = make_client(container).post(
        "/api/auth/login",
        json={"username": "mia", "password": PASSWORD},
        headers={"Origin": ORIGIN},
    )
    assert refused.status_code == 401
    assert _status(owner.patch(f"/api/users/{mia_id}", {"is_active": True})) == 200
    assert login(make_client(container), "mia")
    assert bare.get("/api/projects", headers=bearer).status_code == 200  # the token lives again
    updates = _actions(container, "user.update")
    assert any(u.get("is_active") is False for u in updates)


def test_delete_removes_sessions_tokens_and_grants(
    container: AppContainer, owner: Session, project: str
) -> None:
    mia_id = add_user(container, "mia")
    grant(container, project, mia_id, role=AccessRole.VIEWER)
    mia = sign_in(container, "mia")
    created = container.token_service().create("t", [TokenScope.READ], mia_id, None, SYSTEM)
    assert _status(owner.delete(f"/api/users/{mia_id}")) == 204
    assert _status(mia.get("/api/projects")) == 401
    bearer = {"Authorization": f"Bearer {created.value}"}
    assert (
        TestClient(owner.client.app, base_url=ORIGIN)
        .get("/api/projects", headers=bearer)
        .status_code
        == 401
    )
    assert _status(owner.get(f"/api/users/{mia_id}")) == 404
    access = _json(owner.get(f"/api/projects/{project}/access"))
    assert access["grants"] == []
    assert _actions(container, "user.delete")[-1]["username"] == "mia"


def test_update_profile_and_audit(container: AppContainer, owner: Session) -> None:
    mia_id = add_user(container, "mia")
    response = owner.patch(
        f"/api/users/{mia_id}", {"display_name": "Mia M", "email": "mia@example.org"}
    )
    assert _status(response) == 200
    body = _json(response)
    assert (body["display_name"], body["email"]) == ("Mia M", "mia@example.org")
    assert _status(owner.patch(f"/api/users/{mia_id}", {"email": ""})) == 200
    assert _json(owner.get(f"/api/users/{mia_id}"))["email"] is None
    assert _status(owner.patch("/api/users/9999", {"display_name": "x"})) == 404
    assert _actions(container, "user.update")


def test_session_reports_role_and_permissions(container: AppContainer, owner: Session) -> None:
    body = _json(owner.get("/api/auth/session"))
    assert body["user"]["role"] == "owner"
    assert body["permissions"]["all"] is True
    assert "user.manage" in body["permissions"]["global"]
    add_user(container, "mia")
    mia = sign_in(container, "mia")
    summary = _json(mia.get("/api/auth/session"))["permissions"]
    assert summary["all"] is False and summary["projects"] == {}
    assert "user.manage" not in summary["global"]


def test_change_password_is_rate_limited_after_wrong_current_passwords(
    container: AppContainer, owner: Session
) -> None:
    body = {"current_password": "definitely wrong", "new_password": NEW_PASSWORD}
    for _ in range(5):
        assert _status(owner.post("/api/auth/change-password", body)) == 422
    blocked = owner.post("/api/auth/change-password", body)
    assert _status(blocked) == 429 and "Retry-After" in blocked.headers
    right = {"current_password": PASSWORD, "new_password": NEW_PASSWORD}
    assert _status(owner.post("/api/auth/change-password", right)) == 429
    assert _status(owner.get("/api/projects")) == 200  # only the change is blocked
