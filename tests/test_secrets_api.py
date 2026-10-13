from collections.abc import Iterator, Sequence
from dataclasses import fields, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from backend.adapters.worker_client import WorkerClient
from backend.api.container import AppContainer
from backend.domain.actor import Actor
from backend.domain.enums import ActorType, EnvironmentName
from backend.repositories.database import UnitOfWork
from backend.services.audit import AuditService
from backend.services.secret_cipher import SecretCipher
from backend.services.secrets import SecretInfo
from tests.api_support import (
    FakeClock,
    create_user,
    login,
    make_client,
    make_container,
    make_settings,
    mutation_headers,
    running_worker,
)
from tests.conftest import DEV

BASE = "/api/projects/demo/environments/development/secrets"
VALUE = "TOP-SECRET-VALUE-123"


@pytest.fixture
def wired(
    tmp_path: Path, socket_dir: Path, engine: Engine, clock: FakeClock, project: str
) -> Iterator[AppContainer]:
    """API container talking to a real worker (RPC server with the only cipher)."""
    key_file = tmp_path / "worker-only" / "secret.key"
    with running_worker(socket_dir, engine, key_file) as socket_path:
        settings = make_settings(tmp_path, socket_path=socket_path)
        yield make_container(settings, engine, clock)


@pytest.fixture
def session_client(wired: AppContainer) -> tuple[TestClient, str]:
    create_user(wired)
    client = make_client(wired)
    return client, login(client)


def _token(client: TestClient, csrf: str, scopes: list[str]) -> dict[str, str]:
    body = {"name": "agent", "scopes": scopes}
    response = client.post("/api/tokens", json=body, headers=mutation_headers(client, csrf))
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_set_list_reveal_delete_via_worker(
    wired: AppContainer, session_client: tuple[TestClient, str], engine: Engine
) -> None:
    client, csrf = session_client
    headers = mutation_headers(client, csrf)
    put = client.put(f"{BASE}/DB_PASSWORD", json={"value": VALUE}, headers=headers)
    assert put.status_code == 200
    assert put.json()["name"] == "DB_PASSWORD"
    assert VALUE not in put.text
    listing = client.get(BASE)
    assert [s["name"] for s in listing.json()] == ["DB_PASSWORD"]
    assert VALUE not in listing.text
    reveal = client.post(f"{BASE}/DB_PASSWORD/reveal", headers=headers)
    assert reveal.status_code == 200
    assert reveal.json() == {"name": "DB_PASSWORD", "value": VALUE}
    assert reveal.headers["cache-control"] == "no-store"
    assert client.delete(f"{BASE}/DB_PASSWORD", headers=headers).status_code == 204
    assert client.get(BASE).json() == []


def test_reveal_is_audited_without_value(
    wired: AppContainer, session_client: tuple[TestClient, str]
) -> None:
    client, csrf = session_client
    headers = mutation_headers(client, csrf)
    client.put(f"{BASE}/API_KEY", json={"value": VALUE}, headers=headers)
    client.post(f"{BASE}/API_KEY/reveal", headers=headers)
    audit = AuditService(wired.uow())
    reveals = audit.list(action="secret.reveal")
    assert len(reveals) == 1
    assert reveals[0].actor_type == ActorType.USER
    everything = repr([(e.action, e.details) for e in audit.list()])
    assert VALUE not in everything


def test_ciphertext_in_database_and_key_only_in_worker_volume(
    wired: AppContainer, session_client: tuple[TestClient, str], tmp_path: Path
) -> None:
    client, csrf = session_client
    client.put(f"{BASE}/K", json={"value": VALUE}, headers=mutation_headers(client, csrf))
    uow = UnitOfWork(wired.session_factory)
    with uow:
        env = uow.environments.get_by_project_slug("demo", DEV)
        assert env is not None
        stored = uow.secrets.get(env.id, "K")
        assert stored is not None
        assert VALUE.encode() not in stored.ciphertext
    assert (tmp_path / "worker-only" / "secret.key").exists()
    assert not wired.settings.secret_key_file.exists()
    held = [getattr(wired, field.name) for field in fields(wired)]
    assert not any(isinstance(value, SecretCipher) for value in held)


def test_token_scopes_for_secrets(
    wired: AppContainer, session_client: tuple[TestClient, str]
) -> None:
    client, csrf = session_client
    client.put(f"{BASE}/K", json={"value": VALUE}, headers=mutation_headers(client, csrf))
    reader = _token(client, csrf, ["read"])
    admin = _token(client, csrf, ["admin"])
    bare = TestClient(client.app, base_url=str(client.base_url))
    assert bare.get(BASE, headers=reader).status_code == 200
    assert bare.post(f"{BASE}/K/reveal", headers=reader).status_code == 403
    assert bare.put(f"{BASE}/K", json={"value": "x"}, headers=reader).status_code == 403
    assert bare.delete(f"{BASE}/K", headers=reader).status_code == 403
    revealed = bare.post(f"{BASE}/K/reveal", headers=admin)
    assert revealed.json()["value"] == VALUE
    events = AuditService(wired.uow()).list(action="secret.reveal")
    assert events[0].actor_type == ActorType.TOKEN


def test_unauthenticated_and_csrf(
    wired: AppContainer, session_client: tuple[TestClient, str]
) -> None:
    client, _csrf = session_client
    anonymous = TestClient(client.app, base_url=str(client.base_url))
    assert anonymous.get(BASE).status_code == 401
    origin = {"Origin": "https://app.example.test"}
    assert client.put(f"{BASE}/K", json={"value": "v"}, headers=origin).status_code == 403
    assert client.post(f"{BASE}/K/reveal", headers=origin).status_code == 403
    assert client.delete(f"{BASE}/K", headers=origin).status_code == 403


def test_errors_map_to_http(session_client: tuple[TestClient, str]) -> None:
    client, csrf = session_client
    headers = mutation_headers(client, csrf)
    assert client.get("/api/projects/nope/environments/development/secrets").status_code == 404
    assert client.post(f"{BASE}/MISSING/reveal", headers=headers).status_code == 404
    assert client.get("/api/projects/demo/environments/staging/secrets").status_code == 422
    bad_name = client.put(f"{BASE}/bad-name", json={"value": VALUE}, headers=headers)
    assert bad_name.status_code == 422
    empty = client.put(f"{BASE}/K", json={"value": ""}, headers=headers)
    assert empty.status_code == 422
    extra = client.put(f"{BASE}/K", json={"value": VALUE, "x": 1}, headers=headers)
    assert extra.status_code == 422
    assert VALUE not in extra.text


def test_worker_down_gives_503_without_internals(
    wired: AppContainer, session_client: tuple[TestClient, str], tmp_path: Path
) -> None:
    client, _ = session_client
    down = replace(wired, secrets=WorkerClient(tmp_path / "gone.sock"))
    broken = make_client(down)
    broken.cookies.update(client.cookies)
    response = broken.get(BASE)
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "worker_unavailable"
    assert "gone.sock" not in response.text


class RecordingGateway:
    """A fake worker client proving the API delegates instead of decrypting."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, str, Actor]] = []
        self.values: list[str] = []

    def _record(self, op: str, slug: str, env: EnvironmentName, name: str, actor: Actor) -> None:
        self.calls.append((op, slug, env.value, name, actor))

    def ping(self) -> None:
        return None

    def set_secret(
        self, slug: str, env: EnvironmentName, name: str, value: str, actor: Actor
    ) -> SecretInfo:
        self._record("set", slug, env, name, actor)
        self.values.append(value)
        now = datetime(2026, 1, 1, tzinfo=UTC)
        return SecretInfo(name, now, now)

    def list_secrets(self, slug: str, env: EnvironmentName, actor: Actor) -> Sequence[SecretInfo]:
        self._record("list", slug, env, "", actor)
        return []

    def delete_secret(self, slug: str, env: EnvironmentName, name: str, actor: Actor) -> None:
        self._record("delete", slug, env, name, actor)

    def reveal_secret(self, slug: str, env: EnvironmentName, name: str, actor: Actor) -> str:
        self._record("reveal", slug, env, name, actor)
        return "from-worker"


def test_routes_delegate_to_gateway_with_acting_principal(
    tmp_path: Path, engine: Engine, clock: FakeClock
) -> None:
    gateway = RecordingGateway()
    container = make_container(make_settings(tmp_path), engine, clock, secrets=gateway)
    admin_id = create_user(container)
    client = make_client(container)
    csrf = login(client)
    headers = mutation_headers(client, csrf)
    client.put(f"{BASE}/K", json={"value": "v"}, headers=headers)
    client.get(BASE)
    client.delete(f"{BASE}/K", headers=headers)
    reveal = client.post(f"{BASE}/K/reveal", headers=headers)
    assert reveal.json()["value"] == "from-worker"
    user = Actor(ActorType.USER, str(admin_id))
    assert gateway.values == ["v"]
    assert gateway.calls == [
        ("set", "demo", "development", "K", user),
        ("list", "demo", "development", "", user),
        ("delete", "demo", "development", "K", user),
        ("reveal", "demo", "development", "K", user),
    ]
