from __future__ import annotations

from typing import Any

from backend.inspector import mysql
from backend.inspector.model import ConnectionSpec, Engine, Limits, TlsMode


class _Cursor:
    def __init__(self, statements: list[str]) -> None:
        self.statements = statements

    def __enter__(self) -> _Cursor:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def execute(self, statement: str) -> None:
        self.statements.append(statement)


class _Connection:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def get_server_info(self) -> str:
        return "10.11.8-MariaDB-ubu2204"

    def cursor(self) -> _Cursor:
        return _Cursor(self.statements)


def test_server_banner_selects_mariadb_session_timeout(monkeypatch: Any) -> None:
    connection = _Connection()
    monkeypatch.setattr(mysql, "_connect", lambda *_args: connection)
    spec = ConnectionSpec(Engine.MYSQL, "db", 3306, "app", "reader", "secret", TlsMode.DISABLE)

    mysql.MySqlAdapter(spec, "127.0.0.1", Limits())

    assert any("max_statement_time" in sql for sql in connection.statements)
    assert not any("max_execution_time" in sql for sql in connection.statements)
