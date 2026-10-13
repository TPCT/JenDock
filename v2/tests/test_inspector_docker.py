"""The helper container against real servers on a real docker network (``make test-docker``).

Builds the inspector image from ``docker/inspector.Dockerfile`` (needs network access to PyPI),
creates a throwaway compose-style network ``<project>_default``, attaches a throwaway database
server to it under the alias ``db`` and drives everything through ``InspectorRunner`` exactly as
the worker does. All containers, the network and the image carry ``shiftcodes.test=1`` and are
removed afterwards.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest
from psycopg import sql
from sqlalchemy import Engine as SqlEngine

from shiftcodes.adapters.docker_cli import DockerCliRuntime
from shiftcodes.adapters.inspector_runner import HelperRequest, InspectorRunner
from shiftcodes.domain.enums import EnvironmentName
from shiftcodes.domain.errors import ConflictError
from shiftcodes.inspector.model import ConnectionSpec, Engine, ErrorCode, InspectorError, TlsMode
from shiftcodes.services.environments import EnvironmentService, EnvironmentUpdate
from tests.api_support import (
    FakeClock,
    create_user,
    login,
    make_client,
    make_container,
    make_settings,
    mutation_headers,
)
from tests.docker_support import (
    SESSION_LABEL,
    assert_nothing_left,
    docker_available,
    label_args,
    remove_test_resources,
    session_labels,
)
from tests.inspector_db_support import (
    DATABASE,
    ROOT_PASSWORD,
    RunningDb,
    connect_admin,
    docker_cli,
    seed,
    start_server,
    stop_server,
)
from tests.inspector_support import running_inspection_worker

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not docker_available(), reason="no Docker daemon available"),
]
V2_DIR = Path(__file__).resolve().parents[1]
ENGINES = [Engine.POSTGRESQL, Engine.MYSQL, Engine.MARIADB]
PROJECT = "scit" + secrets.token_hex(3)
NETWORK = f"{PROJECT}_default"
OTHER_NETWORK = f"{PROJECT}x_default"
ALIAS = "db"


@pytest.fixture(scope="module", autouse=True)
def _clean_module() -> Iterator[None]:
    """Created first, torn down last: whatever this module made must be gone."""
    yield
    remove_test_resources()
    assert_nothing_left()


@pytest.fixture(scope="module")
def image() -> Iterator[str]:
    tag = f"shiftcodes-test-inspector:{secrets.token_hex(4)}"
    labels = label_args()
    built = docker_cli(
        "build", "--quiet", *labels, "-f", "docker/inspector.Dockerfile", "-t", tag, str(V2_DIR),
        timeout=900,
    )  # fmt: skip
    assert built.returncode == 0, built.stderr
    yield tag
    docker_cli("rmi", "--force", tag)


@pytest.fixture(scope="module")
def networks() -> Iterator[None]:
    for name in (NETWORK, OTHER_NETWORK):
        created = docker_cli("network", "create", *label_args(), name)
        assert created.returncode == 0, created.stderr
    yield
    for name in (NETWORK, OTHER_NETWORK):
        docker_cli("network", "rm", name)


@pytest.fixture(scope="module", params=ENGINES, ids=[e.value for e in ENGINES])
def db(request: pytest.FixtureRequest, networks: None) -> Iterator[RunningDb]:
    server = start_server(request.param)
    try:
        seed(server)
        joined = docker_cli("network", "connect", "--alias", ALIAS, NETWORK, server.container)
        assert joined.returncode == 0, joined.stderr
        yield server
    finally:
        stop_server(server)


@pytest.fixture(scope="module")
def runner(image: str) -> InspectorRunner:
    runtime = DockerCliRuntime(
        private_dir=Path("/tmp") / f"{PROJECT}-private",  # noqa: S108 - throwaway test dir
        extra_labels=session_labels(),
    )
    return InspectorRunner(runtime, image)


def _spec(db: RunningDb, **changes: Any) -> ConnectionSpec:
    base = db.spec(host=ALIAS, port=db.internal_port)
    return replace(base, **changes)


def _run(
    runner: InspectorRunner, db: RunningDb, op: str, args: dict[str, object] | None = None,
    network: str = NETWORK, **changes: Any,
) -> dict[str, Any]:  # fmt: skip
    return runner.run(network, HelperRequest(op, args or {}, _spec(db, **changes)))


def test_helper_connects_over_the_environment_network_and_reports_a_read_only_server(
    runner: InspectorRunner, db: RunningDb
) -> None:
    info = _run(runner, db, "test")
    assert info["read_only"] is True
    assert info["version"]
    assert info["database"] == DATABASE


def test_metadata_rows_and_console_through_the_helper(
    runner: InspectorRunner, db: RunningDb
) -> None:
    schemas = _run(runner, db, "schemas")["schemas"]
    assert "shop" in [s["name"] for s in schemas]
    tables = _run(runner, db, "tables", {"schema": "shop"})["tables"]
    assert "items" in [t["name"] for t in tables]
    detail = _run(runner, db, "table", {"schema": "shop", "table": "orders"})
    assert detail["foreign_keys"][0]["ref_table"] == "items"
    page = _run(runner, db, "rows", {"schema": "shop", "table": "items", "limit": 3})
    assert [r[0] for r in page["rows"]] == [1, 2, 3]
    assert page["next_after"] == [3]
    follow = _run(
        runner, db, "rows", {"schema": "shop", "table": "items", "limit": 2, "after": [3]}
    )
    assert [r[0] for r in follow["rows"]] == [4, 5]
    result = _run(runner, db, "query", {"statement": "SELECT id FROM shop.items WHERE id < 3"})
    assert result["rows"] == [[1], [2]]


def test_the_guard_runs_in_the_helper_too(runner: InspectorRunner, db: RunningDb) -> None:
    with pytest.raises(InspectorError) as raised:
        _run(runner, db, "query", {"statement": "DELETE FROM shop.nopk"})
    assert raised.value.code is ErrorCode.REJECTED


def test_a_runaway_statement_is_stopped_by_the_five_second_limit(
    runner: InspectorRunner, db: RunningDb
) -> None:
    statement = (
        "SELECT sum(a.id + b.id + c.id + d.id) FROM shop.items a, shop.items b, "
        "shop.items c, shop.items d"
    )
    with pytest.raises(InspectorError) as raised:
        _run(runner, db, "query", {"statement": statement})
    assert raised.value.code is ErrorCode.TIMEOUT


def test_a_huge_result_is_cut_to_the_size_budget(runner: InspectorRunner, db: RunningDb) -> None:
    statement = (
        "SELECT repeat('x', 4000) AS t FROM generate_series(1, 500)"
        if db.engine is Engine.POSTGRESQL
        else "SELECT REPEAT('x', 4000) AS t FROM shop.items"
    )
    result = _run(runner, db, "query", {"statement": statement})
    assert result["size_limited"] is True and result["truncated"] is True
    assert 0 < result["row_count"] < 500
    assert len(json.dumps(result)) < 700 * 1024


def test_connections_outside_the_environment_network_are_refused(
    runner: InspectorRunner, db: RunningDb
) -> None:
    gateway = json.loads(docker_cli("network", "inspect", NETWORK).stdout)[0]["IPAM"]["Config"][0][
        "Gateway"
    ]
    for host in (gateway, "8.8.8.8", "127.0.0.1", "localhost"):
        with pytest.raises(InspectorError) as raised:
            _run(runner, db, "test", host=host)
        assert raised.value.code is ErrorCode.NETWORK_DENIED, host


def test_a_service_on_another_network_is_not_reachable(
    runner: InspectorRunner, db: RunningDb
) -> None:
    with pytest.raises(InspectorError) as raised:
        _run(runner, db, "test", network=OTHER_NETWORK)
    assert raised.value.code is ErrorCode.CONNECT_FAILED
    assert "does not resolve" in raised.value.message


def test_missing_network_is_reported(runner: InspectorRunner, db: RunningDb) -> None:
    with pytest.raises(ConflictError):
        _run(runner, db, "test", network=f"{PROJECT}-nope_default")


def test_wrong_password_is_a_connect_failure_that_does_not_contain_it(
    runner: InspectorRunner, db: RunningDb
) -> None:
    with pytest.raises(InspectorError) as raised:
        _run(runner, db, "test", password="definitely-wrong-pw-1")
    assert raised.value.code is ErrorCode.CONNECT_FAILED
    assert "definitely-wrong-pw-1" not in raised.value.message


def test_tls_require_encrypts_when_the_server_supports_it(
    runner: InspectorRunner, db: RunningDb
) -> None:
    if db.engine is not Engine.POSTGRESQL:
        assert _run(runner, db, "test", tls=TlsMode.REQUIRE)["tls"] is True
    else:
        with pytest.raises(InspectorError) as raised:
            _run(runner, db, "test", tls=TlsMode.REQUIRE)
        assert raised.value.code is ErrorCode.CONNECT_FAILED


SPECIAL_PASSWORD = "p@ss'w\"ord;$(x)\\ #end\nline2"


def _create_special_user(db: RunningDb) -> None:
    connection = connect_admin(db)
    try:
        cursor = connection.cursor()
        if db.engine is Engine.POSTGRESQL:
            query = sql.SQL("CREATE USER sp PASSWORD {}").format(sql.Literal(SPECIAL_PASSWORD))
            cursor.execute(query)
        else:
            cursor.execute("CREATE USER 'sp'@'%%' IDENTIFIED BY %s", (SPECIAL_PASSWORD,))
            cursor.execute("GRANT SELECT ON *.* TO 'sp'@'%'")
    finally:
        connection.close()


def test_passwords_with_special_characters_survive_the_env_file_and_json(
    runner: InspectorRunner, db: RunningDb
) -> None:
    _create_special_user(db)
    info = _run(runner, db, "test", username="sp", password=SPECIAL_PASSWORD)
    assert info["current_user"].startswith("sp")


def test_no_helper_container_is_left_behind(runner: InspectorRunner, db: RunningDb) -> None:
    _run(runner, db, "test")
    assert_no_helper_containers()


# --- the whole chain: API -> worker -> helper container -> database -----------------------


@pytest.fixture
def postgres(db: RunningDb) -> RunningDb:
    if db.engine is not Engine.POSTGRESQL:
        pytest.skip("the full chain runs once, against PostgreSQL")
    return db


def test_full_chain_from_the_api_to_a_real_database(
    runner: InspectorRunner,
    postgres: RunningDb,
    tmp_path: Path,
    socket_dir: Path,
    engine: SqlEngine,
    clock: FakeClock,
    project: str,
) -> None:
    key_file = tmp_path / "worker-only" / "secret.key"
    with running_inspection_worker(socket_dir, engine, key_file, runner) as socket_path:
        container = make_container(make_settings(tmp_path, socket_path=socket_path), engine, clock)
        EnvironmentService(container.uow()).update(
            project, EnvironmentName.DEVELOPMENT, EnvironmentUpdate(compose_project=PROJECT)
        )
        create_user(container)
        client = make_client(container)
        csrf = login(client)
        base = f"/api/projects/{project}/environments/development/databases"
        body = {
            "name": "shop",
            "engine": "postgresql",
            "host": ALIAS,
            "port": postgres.internal_port,
            "database": DATABASE,
            "username": "postgres",
            "password": ROOT_PASSWORD,
        }
        created = client.post(base, json=body, headers=mutation_headers(client, csrf))
        assert created.status_code == 201, created.text
        assert ROOT_PASSWORD not in created.text
        tested = client.post(f"{base}/shop/test", headers=mutation_headers(client, csrf)).json()
        assert tested["ok"] is True, tested
        assert tested["server"]["read_only"] is True
        tables = client.get(f"{base}/shop/schemas/shop/tables").json()["tables"]
        assert "items" in [t["name"] for t in tables]
        page = client.get(f"{base}/shop/schemas/shop/tables/items/rows?limit=2").json()
        assert [r[0] for r in page["rows"]] == [1, 2]
        follow = client.get(
            f"{base}/shop/schemas/shop/tables/items/rows?limit=2&cursor={page['next_cursor']}"
        ).json()
        assert [r[0] for r in follow["rows"]] == [3, 4]
        hostile = 'we"ird %s; DROP TABLE items; --'
        odd = client.get(f"{base}/shop/schemas/shop/tables/{_quote(hostile)}/rows").json()
        assert odd["rows"] == [[1, 10, 20, 30]]
        ok = client.post(
            f"{base}/shop/query",
            json={"statement": "SELECT count(*) AS n FROM shop.items"},
            headers=mutation_headers(client, csrf),
        )
        assert ok.json()["rows"] == [[1200]]
        bad = client.post(
            f"{base}/shop/query",
            json={"statement": "DROP TABLE shop.items"},
            headers=mutation_headers(client, csrf),
        )
        assert bad.status_code == 422
        wrong = client.post(
            f"{base}/shop/query",
            json={"statement": "SELECT * FROM shop.missing"},
            headers=mutation_headers(client, csrf),
        )
        assert wrong.status_code == 422
        assert "missing" in wrong.json()["error"]["message"]
    assert_no_helper_containers()


def _quote(value: str) -> str:
    return quote(value, safe="")


def assert_no_helper_containers() -> None:
    left = docker_cli(
        "ps",
        "--all",
        "--quiet",
        "--filter",
        "label=shiftcodes.inspector=1",
        "--filter",
        f"label={SESSION_LABEL}",
    )
    assert left.stdout.split() == []
