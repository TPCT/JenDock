"""Step 7: the database inspector on the seeded postgres service, read-only by construction."""

from __future__ import annotations

import pytest

from tests.e2e.client import Api
from tests.e2e.scenario import ENV, Scenario
from tests.e2e.stack import Stack

pytestmark = pytest.mark.e2e
DATABASES = f"{ENV}/databases"
ITEMS = f"{DATABASES}/main/schemas/public/tables/items"


def _query(api: Api, statement: str, *, expect: int = 200) -> dict[str, object]:
    result = api.post(f"{DATABASES}/main/query", {"statement": statement}, expect=expect)
    assert isinstance(result, dict)
    return result


def test_12_connection_with_the_password_in_the_secret_store(
    stack: Stack, api: Api, scenario: Scenario
) -> None:
    created = api.post(
        DATABASES,
        {
            "name": "main",
            "engine": "postgresql",
            "host": "db",
            "port": 5432,
            "database": "app",
            "username": "postgres",
            "password": stack.db_password,
            "tls_mode": "disable",
        },
        expect=201,
    )
    assert created["password_secret"] == "DBCONN_MAIN_PASSWORD"
    assert stack.db_password not in str(created)
    assert stack.db_password not in str(api.get(f"{DATABASES}/main"))
    secrets = api.get(f"{ENV}/secrets")
    assert [s["name"] for s in secrets] == ["DBCONN_MAIN_PASSWORD"]
    assert stack.db_password not in str(secrets)
    tested = api.post(f"{DATABASES}/main/test", {})
    assert tested["ok"] is True, tested
    assert tested["server"]["current_user"] == "postgres"
    assert tested["network"] == f"{stack.app_project}_default"


def test_13_schemas_tables_rows_and_a_select(stack: Stack, api: Api, scenario: Scenario) -> None:
    schemas = api.get(f"{DATABASES}/main/schemas")["schemas"]
    assert "public" in [s["name"] for s in schemas]
    tables = api.get(f"{DATABASES}/main/schemas/public/tables")["tables"]
    assert [t["name"] for t in tables] == ["items"]
    detail = api.get(ITEMS)
    assert [c["name"] for c in detail["columns"]] == ["id", "name"]
    rows = api.get(f"{ITEMS}/rows?limit=10")
    assert rows["rows"] == [[1, "alpha"], [2, "beta"], [3, "gamma"]]
    assert rows["ordering"] == "primary_key"
    result = _query(api, "SELECT count(*) AS n FROM items")
    assert result["rows"] == [[3]]
    assert result["statement_kind"] == "SELECT"
    joined = _query(api, "SELECT name FROM items WHERE id > 1 ORDER BY id")
    assert joined["rows"] == [["beta"], ["gamma"]]


def test_14_writes_are_refused(stack: Stack, api: Api, scenario: Scenario) -> None:
    for statement in (
        "INSERT INTO items (name) VALUES ('evil')",
        "DELETE FROM items",
        "UPDATE items SET name = 'x'",
        "DROP TABLE items",
        "SELECT 1; DROP TABLE items",
        "SELECT * INTO copy FROM items",
        "WITH d AS (DELETE FROM items RETURNING *) SELECT * FROM d",
    ):
        _query(api, statement, expect=422)
    assert _query(api, "SELECT count(*) FROM items")["rows"] == [[3]]
    assert api.get(f"{ITEMS}/rows")["rows"][0] == [1, "alpha"]
