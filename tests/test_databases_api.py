"""Database inspector endpoints, end to end through a real worker RPC server (helper faked)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response
from sqlalchemy import Engine

from backend.api.container import AppContainer
from backend.api.schemas.databases import decode_cursor, encode_cursor
from backend.domain.access import AccessRole, GlobalRole
from backend.domain.actor import Actor
from backend.domain.enums import ActorType
from backend.inspector.model import ErrorCode, InspectorError
from backend.repositories.models import AuditEvent
from backend.services.access import AccessService
from backend.services.audit import AuditService
from backend.services.environments import EnvironmentService, EnvironmentUpdate
from backend.services.secret_cipher import SecretCipher
from backend.services.users import UserService
from tests.api_support import (
    FakeClock,
    create_user,
    login,
    make_client,
    make_container,
    make_settings,
    mutation_headers,
)
from tests.conftest import DEV, PROD
from tests.container_support import FakeEngine, make_record
from tests.inspector_support import PAGE, FakeHelper, running_inspection_worker

DEV_BASE = "/api/projects/demo/environments/development/databases"
PROD_BASE = "/api/projects/demo/environments/production/databases"
PASSWORD = "db-Pa$$word-7d41"
BODY = {
    "name": "main",
    "engine": "postgresql",
    "host": "db",
    "database": "app",
    "username": "reader",
    "password": PASSWORD,
}


@pytest.fixture
def helper() -> FakeHelper:
    return FakeHelper()


@pytest.fixture
def fake_container_engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture
def wired(
    tmp_path: Path,
    socket_dir: Path,
    engine: Engine,
    clock: FakeClock,
    project: str,
    helper: FakeHelper,
    fake_container_engine: FakeEngine,
) -> Iterator[AppContainer]:
    key_file = tmp_path / "worker-only" / "secret.key"
    with running_inspection_worker(
        socket_dir, engine, key_file, helper, fake_container_engine
    ) as socket_path:
        container = make_container(make_settings(tmp_path, socket_path=socket_path), engine, clock)
        environments = EnvironmentService(container.uow())
        environments.update(project, DEV, EnvironmentUpdate(compose_project="demo-dev"))
        environments.update(project, PROD, EnvironmentUpdate(compose_project="demo-prod"))
        yield container


@pytest.fixture
def session_client(wired: AppContainer) -> tuple[TestClient, str]:
    create_user(wired)
    client = make_client(wired)
    return client, login(client)


def _token(client: TestClient, csrf: str, scopes: list[str]) -> dict[str, str]:
    body = {"name": "agent", "scopes": scopes}
    response = client.post("/api/tokens", json=body, headers=mutation_headers(client, csrf))
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _create(client: TestClient, csrf: str, base: str = DEV_BASE, **changes: object) -> None:
    body = {**BODY, **changes}
    response = client.post(base, json=body, headers=mutation_headers(client, csrf))
    assert response.status_code == 201, response.text


def _all_audit_events(container: AppContainer) -> list[AuditEvent]:
    """Read every audit page when asserting that a detected password never appears."""
    events: list[AuditEvent] = []
    offset = 0
    while True:
        page = AuditService(container.uow()).list(limit=100, offset=offset)
        events.extend(page)
        if len(page) < 100:
            return events
        offset += len(page)


# --- configuration -------------------------------------------------------------------------


def test_create_stores_the_password_as_a_secret_and_never_returns_it(
    wired: AppContainer, session_client: tuple[TestClient, str]
) -> None:
    client, csrf = session_client
    response = client.post(DEV_BASE, json=BODY, headers=mutation_headers(client, csrf))
    assert response.status_code == 201
    body = response.json()
    assert PASSWORD not in response.text
    assert body["name"] == "main"
    assert body["port"] == 5432
    assert body["tls_mode"] == "disable"
    assert body["password_secret"] == "DBCONN_MAIN_PASSWORD"
    assert body["read_only"] is True
    assert body["notice"] is None
    assert body["last_tested_at"] is None
    secrets = client.get("/api/projects/demo/environments/development/secrets").json()
    assert [s["name"] for s in secrets] == ["DBCONN_MAIN_PASSWORD"]
    revealed = client.post(
        "/api/projects/demo/environments/development/secrets/DBCONN_MAIN_PASSWORD/reveal",
        headers=mutation_headers(client, csrf),
    )
    assert revealed.json()["value"] == PASSWORD
    listing = client.get(DEV_BASE)
    assert listing.status_code == 200
    assert PASSWORD not in listing.text
    assert [c["name"] for c in listing.json()] == ["main"]
    assert client.get(f"{DEV_BASE}/main").json()["host"] == "db"


def test_detect_runs_in_worker_returns_no_password_and_is_idempotent(
    tmp_path: Path,
    wired: AppContainer,
    fake_container_engine: FakeEngine,
    session_client: tuple[TestClient, str],
) -> None:
    client, csrf = session_client
    password = "detected-secret-123"
    fake_container_engine.add(
        make_record(
            project="demo-dev",
            service="db",
            image="postgres:16",
            env={"POSTGRES_PASSWORD": password},
        )
    )
    first = client.post(f"{DEV_BASE}/detect", headers=mutation_headers(client, csrf))
    assert first.status_code == 200, first.text
    assert password not in first.text
    assert first.json()["found"] == 1
    assert first.json()["results"][0]["status"] == "created"
    again = client.post(f"{DEV_BASE}/detect", headers=mutation_headers(client, csrf))
    assert again.status_code == 200
    assert again.json()["results"][0]["status"] == "existing"
    assert len(client.get(DEV_BASE).json()) == 1
    environment = EnvironmentService(wired.uow()).get("demo", DEV)
    with wired.uow() as uow:
        stored = uow.secrets.get(environment.id, "DBCONN_DB_PASSWORD")
        assert stored is not None
        assert (
            SecretCipher(tmp_path / "worker-only" / "secret.key").decrypt(stored.ciphertext)
            == password
        )
    assert password not in first.text and password not in again.text
    events = _all_audit_events(wired)
    assert all(password not in str(event.details) for event in events)


def test_detects_mysql_mariadb_and_postgres_without_exposing_passwords(
    wired: AppContainer,
    fake_container_engine: FakeEngine,
    helper: FakeHelper,
    session_client: tuple[TestClient, str],
) -> None:
    client, csrf = session_client
    containers = [
        (
            "mysql-service",
            "library/mysql:8",
            "mysql-user-secret",
            {
                "MYSQL_DATABASE": "mysql_db",
                "MYSQL_USER": "mysql_user",
                "MYSQL_PASSWORD": "mysql-user-secret",
            },
        ),
        (
            "maria-service",
            "mariadb:11",
            "maria-user-secret",
            {
                "MARIADB_DATABASE": "maria_db",
                "MARIADB_USER": "maria_user",
                "MARIADB_PASSWORD": "maria-user-secret",
            },
        ),
        (
            "pg-service",
            "bitnami/postgresql:16",
            "postgres-secret",
            {
                "POSTGRES_DB": "pg_db",
                "POSTGRES_USER": "pg_user",
                "POSTGRES_PASSWORD": "postgres-secret",
            },
        ),
    ]
    for service, image, _password, env in containers:
        fake_container_engine.add(
            make_record(project="demo-dev", service=service, image=image, env=env)
        )
    fake_container_engine.add(
        make_record(
            project="demo-dev",
            service="stopped-db",
            image="mysql:8",
            running=False,
            env={"MYSQL_DATABASE": "off", "MYSQL_ROOT_PASSWORD": "stopped-secret"},
        )
    )
    fake_container_engine.add(make_record(project="demo-dev", service="cache", image="redis:7"))

    response = client.post(f"{DEV_BASE}/detect", headers=mutation_headers(client, csrf))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["found"] == 3
    assert [item["status"] for item in body["results"]] == ["created"] * 3
    assert [item["connection"]["host"] for item in body["results"]] == [
        item["service"] for item in body["results"]
    ]
    assert all(item["test"]["ok"] for item in body["results"])
    assert [request.operation for _, request in helper.requests].count("test") == 3
    for _, _, password, _ in containers:
        assert password not in response.text
    assert all(
        password not in repr([event.details for event in _all_audit_events(wired)])
        for _, _, password, _ in containers
    )


def test_detect_returns_zero_when_only_unsupported_or_stopped_containers_exist(
    fake_container_engine: FakeEngine,
    session_client: tuple[TestClient, str],
) -> None:
    client, csrf = session_client
    fake_container_engine.add(
        make_record(
            project="demo-dev",
            service="stopped-db",
            image="mysql:8",
            running=False,
            env={"MYSQL_DATABASE": "stopped", "MYSQL_ROOT_PASSWORD": "stopped-secret"},
        )
    )
    fake_container_engine.add(make_record(project="demo-dev", service="cache", image="redis:7"))
    response = client.post(f"{DEV_BASE}/detect", headers=mutation_headers(client, csrf))
    assert response.status_code == 200
    assert response.json() == {"found": 0, "results": [], "removed": [], "kept": []}


def test_detection_authorization_for_members_is_environment_scoped(
    wired: AppContainer, fake_container_engine: FakeEngine
) -> None:
    fake_container_engine.add(
        make_record(
            project="demo-dev",
            service="db",
            image="postgres:16",
            env={"POSTGRES_PASSWORD": "member-safe-password"},
        )
    )
    create_user(wired)

    def member_client(name: str, role: AccessRole | None) -> tuple[TestClient, str]:
        password = "member password 123"
        user = UserService(wired.uow(), wired.hasher, wired.clock).create(
            name, password, Actor.system("test"), role=GlobalRole.MEMBER
        )
        if role is not None:
            AccessService(wired.uow(), wired.clock).set_grant(
                "demo", user.id, Actor.system("test"), environment=DEV, role=role
            )
        member = make_client(wired)
        return member, login(member, name, password)

    viewer, viewer_csrf = member_client("detect-viewer", AccessRole.VIEWER)
    developer, developer_csrf = member_client("detect-developer", AccessRole.DEVELOPER)
    nobody, nobody_csrf = member_client("detect-no-grant", None)
    maintainer, maintainer_csrf = member_client("detect-maintainer", AccessRole.MAINTAINER)

    def request(client: TestClient, csrf: str, base: str) -> Response:
        return client.post(f"{base}/detect", headers=mutation_headers(client, csrf))

    assert request(viewer, viewer_csrf, DEV_BASE).status_code == 403
    assert request(developer, developer_csrf, DEV_BASE).status_code == 403
    assert request(nobody, nobody_csrf, DEV_BASE).status_code == 404
    assert request(maintainer, maintainer_csrf, DEV_BASE).status_code == 200
    assert (
        request(maintainer, maintainer_csrf, PROD_BASE).status_code == 404
    )  # a development-only grant cannot see production


def test_detection_route_role_matrix(
    wired: AppContainer,
    fake_container_engine: FakeEngine,
) -> None:
    fake_container_engine.add(
        make_record(
            project="demo-dev",
            service="db",
            image="postgres:16",
            env={"POSTGRES_PASSWORD": "maintainer-db-password"},
        )
    )
    owner_id = create_user(wired)
    del owner_id

    def member_request(username: str, role: AccessRole | None) -> tuple[TestClient, str]:
        password = "member password 123"
        user = UserService(wired.uow(), wired.hasher, wired.clock).create(
            username, password, Actor.system("test"), role=GlobalRole.MEMBER
        )
        if role is not None:
            AccessService(wired.uow(), wired.clock).set_grant(
                "demo", user.id, Actor.system("test"), environment=DEV, role=role
            )
        client = make_client(wired)
        return client, login(client, username, password)

    viewer, viewer_csrf = member_request("viewer-detect", AccessRole.VIEWER)
    developer, developer_csrf = member_request("developer-detect", AccessRole.DEVELOPER)
    no_grant, no_grant_csrf = member_request("no-grant-detect", None)
    maintainer, maintainer_csrf = member_request("maintainer-detect", AccessRole.MAINTAINER)
    assert (
        viewer.post(f"{DEV_BASE}/detect", headers=mutation_headers(viewer, viewer_csrf)).status_code
        == 403
    )
    assert (
        developer.post(
            f"{DEV_BASE}/detect", headers=mutation_headers(developer, developer_csrf)
        ).status_code
        == 403
    )
    assert (
        no_grant.post(
            f"{DEV_BASE}/detect", headers=mutation_headers(no_grant, no_grant_csrf)
        ).status_code
        == 404
    )
    assert (
        maintainer.post(
            f"{DEV_BASE}/detect", headers=mutation_headers(maintainer, maintainer_csrf)
        ).status_code
        == 200
    )
    assert (
        maintainer.post(
            f"{PROD_BASE}/detect", headers=mutation_headers(maintainer, maintainer_csrf)
        ).status_code
        == 404
    )


def test_production_connection_carries_the_read_only_user_notice(
    session_client: tuple[TestClient, str],
) -> None:
    client, csrf = session_client
    _create(client, csrf, PROD_BASE, engine="mariadb", port=3307)
    body = client.get(f"{PROD_BASE}/main").json()
    assert body["engine"] == "mariadb"
    assert body["port"] == 3307
    assert body["read_only"] is True
    assert "SELECT" in body["notice"]


def test_create_is_audited_without_the_password(
    wired: AppContainer, session_client: tuple[TestClient, str]
) -> None:
    client, csrf = session_client
    _create(client, csrf)
    events = AuditService(wired.uow()).list(action="database.connection.create")
    assert len(events) == 1
    assert events[0].actor_type is ActorType.USER
    assert events[0].details["host"] == "db"
    assert PASSWORD not in repr([(e.action, e.details) for e in AuditService(wired.uow()).list()])


@pytest.mark.parametrize(
    "changes",
    [
        {"name": "Bad Name"},
        {"name": ""},
        {"host": "localhost"},
        {"host": "127.0.0.1"},
        {"host": "169.254.169.254"},
        {"host": "bad host"},
        {"port": 0},
        {"port": 70000},
        {"engine": "oracle"},
        {"tls_mode": "verify-full"},
        {"password": ""},
        {"unknown": 1},
    ],
)
def test_create_validates_input(
    session_client: tuple[TestClient, str], changes: dict[str, object]
) -> None:
    client, csrf = session_client
    response = client.post(
        DEV_BASE, json={**BODY, **changes}, headers=mutation_headers(client, csrf)
    )
    assert response.status_code == 422
    assert PASSWORD not in response.text


def test_duplicate_name_conflicts_and_leaves_one_secret(
    session_client: tuple[TestClient, str],
) -> None:
    client, csrf = session_client
    _create(client, csrf)
    again = client.post(DEV_BASE, json=BODY, headers=mutation_headers(client, csrf))
    assert again.status_code == 409
    secrets = client.get("/api/projects/demo/environments/development/secrets").json()
    assert len(secrets) == 1


def test_connection_limit_per_environment(session_client: tuple[TestClient, str]) -> None:
    client, csrf = session_client
    for number in range(10):
        _create(client, csrf, name=f"c{number}")
    response = client.post(
        DEV_BASE, json={**BODY, "name": "one-too-many"}, headers=mutation_headers(client, csrf)
    )
    assert response.status_code == 409


def test_update_changes_fields_and_rotates_the_password(
    wired: AppContainer, session_client: tuple[TestClient, str]
) -> None:
    client, csrf = session_client
    _create(client, csrf)
    headers = mutation_headers(client, csrf)
    response = client.patch(
        f"{DEV_BASE}/main",
        json={"host": "db2", "port": 6543, "password": "rotated-pass"},
        headers=headers,
    )
    assert response.status_code == 200
    assert (response.json()["host"], response.json()["port"]) == ("db2", 6543)
    assert "rotated-pass" not in response.text
    revealed = client.post(
        "/api/projects/demo/environments/development/secrets/DBCONN_MAIN_PASSWORD/reveal",
        headers=headers,
    )
    assert revealed.json()["value"] == "rotated-pass"
    event = AuditService(wired.uow()).list(action="database.connection.update")[0]
    assert event.details["changed"] == ["host", "password", "port"]
    assert "rotated-pass" not in repr(event.details)
    assert client.patch(f"{DEV_BASE}/nope", json={"host": "x"}, headers=headers).status_code == 404
    assert (
        client.patch(f"{DEV_BASE}/main", json={"host": "::1"}, headers=headers).status_code == 422
    )


def test_delete_removes_the_connection_and_its_secret(
    session_client: tuple[TestClient, str],
) -> None:
    client, csrf = session_client
    _create(client, csrf)
    headers = mutation_headers(client, csrf)
    assert client.delete(f"{DEV_BASE}/main", headers=headers).status_code == 204
    assert client.get(f"{DEV_BASE}/main").status_code == 404
    assert client.get("/api/projects/demo/environments/development/secrets").json() == []
    assert client.delete(f"{DEV_BASE}/main", headers=headers).status_code == 404


def test_connections_are_per_environment(session_client: tuple[TestClient, str]) -> None:
    client, csrf = session_client
    _create(client, csrf)
    assert client.get(PROD_BASE).json() == []
    assert client.get(f"{PROD_BASE}/main").status_code == 404


# --- scopes --------------------------------------------------------------------------------


def test_scopes_for_configuration_metadata_rows_and_console(
    helper: FakeHelper, session_client: tuple[TestClient, str]
) -> None:
    client, csrf = session_client
    _create(client, csrf)
    _create(client, csrf, PROD_BASE)
    reader = _token(client, csrf, ["read"])
    admin = _token(client, csrf, ["admin"])
    bare = TestClient(client.app, base_url=str(client.base_url))
    statement = {"statement": "SELECT 1"}
    tables = "schemas/public/tables"
    # read: configuration is visible, metadata and development data are readable
    assert bare.get(DEV_BASE, headers=reader).status_code == 200
    assert bare.get(f"{DEV_BASE}/main", headers=reader).status_code == 200
    assert bare.get(f"{DEV_BASE}/main/schemas", headers=reader).status_code == 200
    assert bare.get(f"{DEV_BASE}/main/{tables}", headers=reader).status_code == 200
    assert bare.get(f"{DEV_BASE}/main/{tables}/users", headers=reader).status_code == 200
    assert bare.get(f"{DEV_BASE}/main/{tables}/users/rows", headers=reader).status_code == 200
    assert bare.post(f"{DEV_BASE}/main/query", json=statement, headers=reader).status_code == 200
    # read: production metadata yes, production rows and console no
    assert bare.get(f"{PROD_BASE}/main/{tables}/users", headers=reader).status_code == 200
    assert bare.get(f"{PROD_BASE}/main/{tables}/users/rows", headers=reader).status_code == 403
    assert bare.post(f"{PROD_BASE}/main/query", json=statement, headers=reader).status_code == 403
    # read: no configuration changes and no connection test
    assert bare.post(DEV_BASE, json=BODY, headers=reader).status_code == 403
    assert bare.patch(f"{DEV_BASE}/main", json={"host": "x"}, headers=reader).status_code == 403
    assert bare.delete(f"{DEV_BASE}/main", headers=reader).status_code == 403
    assert bare.post(f"{DEV_BASE}/main/test", headers=reader).status_code == 403
    # admin: production data and the test endpoint
    assert bare.get(f"{PROD_BASE}/main/{tables}/users/rows", headers=admin).status_code == 200
    assert bare.post(f"{PROD_BASE}/main/query", json=statement, headers=admin).status_code == 200
    assert bare.post(f"{DEV_BASE}/main/test", headers=admin).status_code == 200
    assert helper.requests


def test_unauthenticated_and_csrf(session_client: tuple[TestClient, str]) -> None:
    client, _csrf = session_client
    anonymous = TestClient(client.app, base_url=str(client.base_url))
    assert anonymous.get(DEV_BASE).status_code == 401
    assert (
        anonymous.post(f"{DEV_BASE}/main/query", json={"statement": "SELECT 1"}).status_code == 401
    )
    origin = {"Origin": "https://app.example.test"}
    assert client.post(DEV_BASE, json=BODY, headers=origin).status_code == 403
    assert (
        client.post(f"{DEV_BASE}/main/query", json={"statement": "SELECT 1"}, headers=origin)
    ).status_code == 403


# --- connection test -----------------------------------------------------------------------


def test_test_endpoint_reports_the_server_and_records_the_result(
    helper: FakeHelper, session_client: tuple[TestClient, str]
) -> None:
    client, csrf = session_client
    _create(client, csrf)
    response = client.post(f"{DEV_BASE}/main/test", headers=mutation_headers(client, csrf))
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["server"]["read_only"] is True
    assert body["network"] == "demo-dev_default"
    assert PASSWORD not in response.text
    stored = client.get(f"{DEV_BASE}/main").json()
    assert stored["last_test_ok"] is True
    assert stored["last_tested_at"] is not None
    network, request = helper.requests[0]
    assert network == "demo-dev_default"
    assert request.connection.password == PASSWORD
    assert (request.connection.host, request.connection.port) == ("db", 5432)


def test_test_endpoint_uses_a_saved_custom_network(
    wired: AppContainer,
    helper: FakeHelper,
    session_client: tuple[TestClient, str],
) -> None:
    client, csrf = session_client
    _create(client, csrf)
    with wired.uow() as uow:
        environment = uow.environments.get_by_project_slug("demo", DEV)
        assert environment is not None
        connection = uow.database_connections.get(environment.id, "main")
        assert connection is not None
        connection.network = "octa_data"
    response = client.post(f"{DEV_BASE}/main/test", headers=mutation_headers(client, csrf))
    assert response.status_code == 200
    assert helper.requests[0][0] == "octa_data"


def test_failed_test_is_a_200_with_ok_false(
    helper: FakeHelper, session_client: tuple[TestClient, str]
) -> None:
    client, csrf = session_client
    _create(client, csrf)
    helper.failure = InspectorError(ErrorCode.CONNECT_FAILED, "connection refused")
    response = client.post(f"{DEV_BASE}/main/test", headers=mutation_headers(client, csrf))
    assert response.status_code == 200
    assert response.json()["ok"] is False
    assert response.json()["error"] == {"code": "connect_failed", "message": "connection refused"}
    assert client.get(f"{DEV_BASE}/main").json()["last_test_ok"] is False


def test_environment_without_compose_project_cannot_connect(
    wired: AppContainer, session_client: tuple[TestClient, str]
) -> None:
    client, csrf = session_client
    _create(client, csrf)
    EnvironmentService(wired.uow()).update(
        "demo", DEV, EnvironmentUpdate(clear_compose_project=True)
    )
    result = client.post(f"{DEV_BASE}/main/test", headers=mutation_headers(client, csrf)).json()
    assert result["ok"] is False
    assert result["error"]["code"] == "network_unavailable"
    assert client.get(f"{DEV_BASE}/main/schemas").status_code == 409


# --- metadata and rows ---------------------------------------------------------------------


def test_metadata_shapes(session_client: tuple[TestClient, str]) -> None:
    client, csrf = session_client
    _create(client, csrf)
    assert client.get(f"{DEV_BASE}/main/schemas").json() == {
        "schemas": [{"name": "public", "system": False}]
    }
    tables = client.get(f"{DEV_BASE}/main/schemas/public/tables").json()
    assert tables["tables"][0] == {
        "schema": "public",
        "name": "users",
        "kind": "table",
        "approx_rows": 42,
    }
    detail = client.get(f"{DEV_BASE}/main/schemas/public/tables/users").json()
    assert detail["columns"][0]["primary_key_position"] == 1
    assert detail["indexes"][0]["primary"] is True
    assert detail["foreign_keys"] == []
    assert detail["table"]["name"] == "users"


def test_include_system_flag_reaches_the_helper(
    helper: FakeHelper, session_client: tuple[TestClient, str]
) -> None:
    client, csrf = session_client
    _create(client, csrf)
    client.get(f"{DEV_BASE}/main/schemas?include_system=true")
    assert helper.requests[-1][1].args == {"include_system": True}


def test_rows_paging_uses_an_opaque_cursor_and_forwards_it(
    helper: FakeHelper, session_client: tuple[TestClient, str]
) -> None:
    client, csrf = session_client
    _create(client, csrf)
    url = f"{DEV_BASE}/main/schemas/public/tables/users/rows"
    first = client.get(f"{url}?limit=2").json()
    assert first["rows"] == PAGE["rows"]
    assert first["next_offset"] == 2
    assert "next_after" not in first
    assert decode_cursor(first["next_cursor"]) == [2]
    assert helper.requests[-1][1].args == {
        "schema": "public",
        "table": "users",
        "limit": 2,
        "offset": 0,
        "after": None,
    }
    client.get(f"{url}?limit=2&cursor={first['next_cursor']}")
    assert helper.requests[-1][1].args["after"] == [2]


@pytest.mark.parametrize("query", ["limit=0", "limit=501", "offset=-1", "cursor=!!!", "cursor=W10"])
def test_rows_reject_bad_paging_parameters(
    session_client: tuple[TestClient, str], query: str
) -> None:
    client, csrf = session_client
    _create(client, csrf)
    response = client.get(f"{DEV_BASE}/main/schemas/public/tables/users/rows?{query}")
    assert response.status_code == 422


def test_cursor_round_trip_and_validation() -> None:
    values: list[object] = [1, "a b", "é"]
    assert decode_cursor(encode_cursor(values)) == values
    for bad in ("", "!!", encode_cursor([]), encode_cursor([{"a": 1}]), encode_cursor([True])):
        with pytest.raises(Exception, match="invalid cursor"):
            decode_cursor(bad)


def test_hostile_names_in_the_url_are_only_data(
    helper: FakeHelper, session_client: tuple[TestClient, str]
) -> None:
    client, csrf = session_client
    _create(client, csrf)
    client.get(f"{DEV_BASE}/main/schemas/pub%22lic/tables/we%22ird%3B%20DROP%20TABLE%20x/rows")
    args = helper.requests[-1][1].args
    assert args["schema"] == 'pub"lic'
    assert args["table"] == 'we"ird; DROP TABLE x'


# --- console -------------------------------------------------------------------------------


def test_console_result_shape_and_audit_has_statement_but_never_results(
    wired: AppContainer, helper: FakeHelper, session_client: tuple[TestClient, str]
) -> None:
    client, csrf = session_client
    _create(client, csrf)
    statement = "SELECT 7777 AS n"
    response = client.post(
        f"{DEV_BASE}/main/query",
        json={"statement": statement},
        headers=mutation_headers(client, csrf),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["rows"] == [[7777]]
    assert body["row_count"] == 1 and body["limit"] == 500 and body["truncated"] is False
    events = AuditService(wired.uow()).list(action="database.query")
    assert len(events) == 1
    assert events[0].details["statement"] == statement
    assert events[0].details["outcome"] == "ok"
    assert events[0].details["rows"] == 1
    assert "7777" not in repr({k: v for k, v in events[0].details.items() if k != "statement"})
    assert helper.requests[-1][1].args == {"statement": statement}


def test_console_rejects_writes_before_any_container_starts(
    wired: AppContainer, helper: FakeHelper, session_client: tuple[TestClient, str]
) -> None:
    client, csrf = session_client
    _create(client, csrf)
    response = client.post(
        f"{DEV_BASE}/main/query",
        json={"statement": "DELETE FROM users"},
        headers=mutation_headers(client, csrf),
    )
    assert response.status_code == 422
    assert "SELECT, WITH, SHOW and EXPLAIN" in response.json()["error"]["message"]
    assert helper.requests == []
    event = AuditService(wired.uow()).list(action="database.query")[0]
    assert event.details["outcome"] == "rejected"
    assert event.details["statement"] == "DELETE FROM users"


def test_console_timeout_is_reported_and_audited(
    wired: AppContainer, helper: FakeHelper, session_client: tuple[TestClient, str]
) -> None:
    client, csrf = session_client
    _create(client, csrf)
    helper.failure = InspectorError(ErrorCode.TIMEOUT, "statement exceeded the time limit")
    response = client.post(
        f"{DEV_BASE}/main/query",
        json={"statement": "SELECT 1"},
        headers=mutation_headers(client, csrf),
    )
    assert response.status_code == 422
    assert "time limit" in response.json()["error"]["message"]
    assert (
        AuditService(wired.uow()).list(action="database.query")[0].details["outcome"] == "timeout"
    )


def test_production_row_reads_are_audited_and_development_reads_are_not(
    wired: AppContainer, session_client: tuple[TestClient, str]
) -> None:
    client, csrf = session_client
    _create(client, csrf)
    _create(client, csrf, PROD_BASE)
    client.get(f"{DEV_BASE}/main/schemas/public/tables/users/rows")
    assert AuditService(wired.uow()).list(action="database.rows.read") == []
    client.get(f"{PROD_BASE}/main/schemas/public/tables/users/rows")
    events = AuditService(wired.uow()).list(action="database.rows.read")
    assert len(events) == 1
    assert events[0].details["table"] == "users"


def test_unknown_connection_is_404(session_client: tuple[TestClient, str]) -> None:
    client, csrf = session_client
    assert client.get(f"{DEV_BASE}/nope/schemas").status_code == 404
    response = client.post(
        f"{DEV_BASE}/nope/query",
        json={"statement": "SELECT 1"},
        headers=mutation_headers(client, csrf),
    )
    assert response.status_code == 404


def test_missing_worker_is_a_503(
    tmp_path: Path, engine: Engine, clock: FakeClock, project: str
) -> None:
    container = make_container(make_settings(tmp_path), engine, clock)
    create_user(container)
    client = make_client(container)
    login(client)
    assert client.get(f"{DEV_BASE}/main/schemas").status_code == 503
