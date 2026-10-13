from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.api.container import AppContainer
from backend.domain.actor import Actor
from backend.domain.enums import ActorType, TokenScope
from backend.domain.errors import ValidationError
from backend.repositories.models import ApiToken
from backend.services.audit import AuditService
from backend.services.tokens import LAST_USED_RESOLUTION, TOKEN_PREFIX, TokenService
from tests.api_support import FakeClock, login, make_client, mutation_headers

ORIGIN = {"Origin": "https://app.example.test"}
# The default test container has no worker: an authorized request reaches it and gets a 503.
WORKER_DOWN = 503


def _create(client: TestClient, csrf: str, scopes: list[str], **extra: object) -> dict[str, object]:
    body = {"name": "agent", "scopes": scopes, **extra}
    response = client.post("/api/tokens", json=body, headers=mutation_headers(client, csrf))
    assert response.status_code == 201, response.text
    data: dict[str, object] = response.json()
    return data


def _bearer(value: object) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


@pytest.fixture
def csrf(client: TestClient, admin: int) -> str:
    return login(client)


def test_create_shows_secret_once_and_stores_only_hash(
    container: AppContainer, client: TestClient, csrf: str
) -> None:
    created = _create(client, csrf, ["read"])
    value = created["token"]
    assert isinstance(value, str)
    assert value.startswith(TOKEN_PREFIX)
    assert len(value) >= len(TOKEN_PREFIX) + 43
    listing = client.get("/api/tokens")
    assert value not in listing.text
    assert listing.json()["items"][0]["prefix"] == value[:8]
    uow = container.uow()
    with uow:
        row = uow.session.scalars(select(ApiToken)).one()
        assert value not in (row.token_hash, row.token_prefix, repr(row.scopes))
        assert len(row.token_hash) == 64
    audit = repr([e.details for e in AuditService(container.uow()).list()])
    assert value not in audit


def test_token_authenticates_with_scope_and_sessionless(client: TestClient, csrf: str) -> None:
    value = _create(client, csrf, ["read"])["token"]
    bare = TestClient(client.app, base_url=str(client.base_url))
    path = "/api/projects/demo/environments/development/secrets"
    assert bare.get(path, headers=_bearer(value)).status_code == WORKER_DOWN


def test_scope_enforced(client: TestClient, csrf: str, container: AppContainer) -> None:
    value = _create(client, csrf, ["read"])["token"]
    bare = TestClient(client.app, base_url=str(client.base_url))
    path = "/api/projects/demo/environments/development/secrets/DB"
    response = bare.put(path, json={"value": "v"}, headers={**_bearer(value), **ORIGIN})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


def test_admin_scope_implies_others(client: TestClient, csrf: str) -> None:
    value = _create(client, csrf, ["admin"])["token"]
    bare = TestClient(client.app, base_url=str(client.base_url))
    path = "/api/projects/demo/environments/development/secrets"
    assert bare.get(path, headers=_bearer(value)).status_code == WORKER_DOWN


def test_token_cannot_manage_tokens(client: TestClient, csrf: str) -> None:
    value = _create(client, csrf, ["admin", "read"])["token"]
    bare = TestClient(client.app, base_url=str(client.base_url))
    headers = _bearer(value)
    assert bare.get("/api/tokens", headers=headers).status_code == 403
    assert (
        bare.post(
            "/api/tokens", json={"name": "x", "scopes": ["read"]}, headers=headers
        ).status_code
        == 403
    )
    assert bare.delete("/api/tokens/1", headers=headers).status_code == 403


def test_bearer_requires_no_csrf_but_never_falls_back_to_cookie(
    client: TestClient, csrf: str
) -> None:
    value = _create(client, csrf, ["read"])["token"]
    # the client carries a valid session cookie; an invalid bearer must not fall back to it
    response = client.get("/api/auth/session", headers=_bearer("jdt_invalid"))
    assert response.status_code == 401
    response = client.get("/api/tokens", headers=_bearer(value))
    assert response.status_code == 403  # token principal, not the cookie session


def test_bearer_scheme_must_be_bearer(client: TestClient, csrf: str) -> None:
    assert client.get("/api/tokens", headers={"Authorization": "Basic abc"}).status_code == 401


def test_revoked_token_rejected(client: TestClient, csrf: str) -> None:
    created = _create(client, csrf, ["read"])
    info = created["info"]
    assert isinstance(info, dict)
    revoke = client.delete(f"/api/tokens/{info['id']}", headers=mutation_headers(client, csrf))
    assert revoke.status_code == 200
    assert revoke.json()["revoked_at"] is not None
    bare = TestClient(client.app, base_url=str(client.base_url))
    path = "/api/projects/demo/environments/development/secrets"
    assert bare.get(path, headers=_bearer(created["token"])).status_code == 401
    again = client.delete(f"/api/tokens/{info['id']}", headers=mutation_headers(client, csrf))
    assert again.status_code == 200


def test_revoke_unknown_token_is_404(client: TestClient, csrf: str) -> None:
    response = client.delete("/api/tokens/999", headers=mutation_headers(client, csrf))
    assert response.status_code == 404


def test_expired_token_rejected(client: TestClient, csrf: str, clock: FakeClock) -> None:
    expires = (clock.now + timedelta(hours=1)).isoformat()
    value = _create(client, csrf, ["read"], expires_at=expires)["token"]
    bare = TestClient(client.app, base_url=str(client.base_url))
    path = "/api/projects/demo/environments/development/secrets"
    assert bare.get(path, headers=_bearer(value)).status_code == WORKER_DOWN
    clock.advance(hours=2)
    assert bare.get(path, headers=_bearer(value)).status_code == 401


def test_create_validation(client: TestClient, csrf: str, clock: FakeClock) -> None:
    headers = mutation_headers(client, csrf)
    assert (
        client.post("/api/tokens", json={"name": "x", "scopes": []}, headers=headers).status_code
        == 422
    )
    assert (
        client.post(
            "/api/tokens", json={"name": "x", "scopes": ["bogus"]}, headers=headers
        ).status_code
        == 422
    )
    past = (clock.now - timedelta(hours=1)).isoformat()
    body = {"name": "x", "scopes": ["read"], "expires_at": past}
    assert client.post("/api/tokens", json=body, headers=headers).status_code == 422
    naive = {"name": "x", "scopes": ["read"], "expires_at": "2030-01-01T00:00:00"}
    assert client.post("/api/tokens", json=naive, headers=headers).status_code == 422


def test_token_management_requires_csrf(client: TestClient, csrf: str) -> None:
    body = {"name": "x", "scopes": ["read"]}
    assert client.post("/api/tokens", json=body, headers=ORIGIN).status_code == 403


def test_unknown_or_malformed_tokens_rejected(client: TestClient, admin: int) -> None:
    bare = TestClient(client.app, base_url=str(client.base_url))
    for value in ("jdt_" + "a" * 43, "nope", "jdt_" + "a" * 500, ""):
        assert bare.get("/api/auth/session", headers=_bearer(value)).status_code == 401


def test_last_used_updated_at_most_once_per_minute(
    container: AppContainer, clock: FakeClock, admin: int
) -> None:
    service = TokenService(container.uow(), clock)
    created = service.create("a", [TokenScope.READ], admin, None, Actor(ActorType.USER, "1"))
    assert created.info.last_used_at is None
    assert service.authenticate(created.value) is not None
    first = service.list()[0].last_used_at
    assert first == clock.now
    clock.advance(seconds=30)
    service.authenticate(created.value)
    assert service.list()[0].last_used_at == first
    clock.advance(seconds=LAST_USED_RESOLUTION.total_seconds())
    service.authenticate(created.value)
    assert service.list()[0].last_used_at == clock.now


def test_service_rejects_empty_scopes(container: AppContainer, admin: int) -> None:
    service = container.token_service()
    with pytest.raises(ValidationError):
        service.create("a", [], admin, None, Actor(ActorType.USER, "1"))


def test_token_actions_audited_with_user_actor_and_token_actor(
    container: AppContainer, client: TestClient, csrf: str, admin: int
) -> None:
    created = _create(client, csrf, ["read"])
    info = created["info"]
    assert isinstance(info, dict)
    events = AuditService(container.uow()).list(action="token.create")
    assert (events[0].actor_type, events[0].actor_id) == (ActorType.USER, str(admin))
    assert events[0].details["scopes"] == ["read"]
    assert events[0].target_id == str(info["id"])
    assert Actor(ActorType.TOKEN, str(info["id"])).type == ActorType.TOKEN


def test_make_client_helper_isolated(container: AppContainer) -> None:
    assert make_client(container).get("/healthz").status_code == 200


def test_token_list_is_paged_searchable_and_filterable_by_state(
    client: TestClient, csrf: str
) -> None:
    ids = []
    for name in ("deploy bot", "ci runner", "audit reader"):
        body = {"name": name, "scopes": ["read"]}
        response = client.post("/api/tokens", json=body, headers=mutation_headers(client, csrf))
        ids.append(response.json()["info"]["id"])
    client.delete(f"/api/tokens/{ids[0]}", headers=mutation_headers(client, csrf))
    page = client.get("/api/tokens", params={"per_page": 2}).json()
    assert (page["total"], page["pages"]) == (3, 2)
    assert [t["id"] for t in page["items"]] == [ids[2], ids[1]]
    runner = client.get("/api/tokens", params={"q": "RUNNER"}).json()
    assert [t["name"] for t in runner["items"]] == ["ci runner"]
    revoked = client.get("/api/tokens", params={"state": "revoked"}).json()
    assert [t["id"] for t in revoked["items"]] == [ids[0]]
    assert client.get("/api/tokens", params={"state": "active"}).json()["total"] == 2
    by_name = client.get("/api/tokens", params={"sort": "name", "per_page": 1}).json()
    assert by_name["items"][0]["name"] == "audit reader"
    assert client.get("/api/tokens", params={"state": "bogus"}).status_code == 422
