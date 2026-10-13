"""Throwaway database servers for the inspector tests (``make test-docker``).

Pinned images, every container labelled ``shiftcodes.test=1`` and removed again. The servers
publish their port on 127.0.0.1 only. ``seed`` builds the same logical schema in all engines,
including tables and columns with hostile names.
"""

from __future__ import annotations

import secrets
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any

import psycopg
import pymysql

from shiftcodes.inspector.model import ConnectionSpec, Engine, TlsMode
from tests.docker_support import label_args

ROOT_PASSWORD = "Root-Pass-" + secrets.token_hex(6)
DATABASE = "inspectdb"
IMAGES = {
    Engine.POSTGRESQL: ("postgres:16", 5432),
    Engine.MYSQL: ("mysql:8.4", 3306),
    Engine.MARIADB: ("mariadb:11.4", 3306),
}
READY_TIMEOUT = 120.0
BIG_TEXT_LENGTH = 10_000
BINARY_LENGTH = 300
ITEM_COUNT = 1200
HOSTILE_PG = 'we"ird %s; DROP TABLE items; --'
HOSTILE_MYSQL = "we`ird %s; DROP TABLE items; --"
HOSTILE_COLUMNS_PG = ["select", 'a"b', "%s"]
HOSTILE_COLUMNS_MYSQL = ["select", "a`b", "%s"]


def docker_cli(*args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    docker = shutil.which("docker")
    assert docker is not None
    return subprocess.run(  # noqa: S603 - fixed argv in tests
        [docker, *args], capture_output=True, text=True, timeout=timeout, check=False
    )


@dataclass(slots=True)
class RunningDb:
    """A started server: how to reach it from the host and from a docker network."""

    engine: Engine
    container: str
    host_port: int

    def spec(self, host: str = "127.0.0.1", port: int | None = None) -> ConnectionSpec:
        """Connection details (as root of the throwaway server)."""
        return ConnectionSpec(
            engine=self.engine,
            host=host,
            port=self.host_port if port is None else port,
            database=DATABASE,
            username="postgres" if self.engine is Engine.POSTGRESQL else "root",
            password=ROOT_PASSWORD,
            tls=TlsMode.DISABLE,
        )

    @property
    def internal_port(self) -> int:
        """The port inside the container."""
        return IMAGES[self.engine][1]


def start_server(engine: Engine) -> RunningDb:
    """Start a labelled container for ``engine`` and wait until it accepts connections."""
    image, port = IMAGES[engine]
    name = f"sc-test-{engine.value}-{secrets.token_hex(4)}"
    env = _server_env(engine)
    args = ["run", "-d", "--name", name, *label_args()]
    args += ["-p", f"127.0.0.1::{port}"]
    for key, value in env.items():
        args += ["-e", f"{key}={value}"]
    result = docker_cli(*args, image)
    assert result.returncode == 0, result.stderr
    mapped = docker_cli("port", name, f"{port}/tcp").stdout.split(":")[-1].strip()
    db = RunningDb(engine, name, int(mapped))
    _wait_ready(db)
    return db


def _server_env(engine: Engine) -> dict[str, str]:
    if engine is Engine.POSTGRESQL:
        return {"POSTGRES_PASSWORD": ROOT_PASSWORD, "POSTGRES_DB": DATABASE}
    if engine is Engine.MYSQL:
        return {"MYSQL_ROOT_PASSWORD": ROOT_PASSWORD, "MYSQL_DATABASE": DATABASE}
    return {"MARIADB_ROOT_PASSWORD": ROOT_PASSWORD, "MARIADB_DATABASE": DATABASE}


def _wait_ready(db: RunningDb) -> None:
    deadline = time.monotonic() + READY_TIMEOUT
    consecutive = 0
    while time.monotonic() < deadline:
        try:
            connection = connect_admin(db)
            connection.close()
            consecutive += 1
            if consecutive >= 2:
                return
        except (psycopg.Error, pymysql.Error, OSError):
            consecutive = 0
        time.sleep(1)
    logs = docker_cli("logs", "--tail", "20", db.container)
    raise AssertionError(f"{db.engine.value} did not become ready: {logs.stdout} {logs.stderr}")


def connect_admin(db: RunningDb) -> Any:
    """An autocommit admin connection used to seed and to prepare special cases."""
    spec = db.spec()
    if db.engine is Engine.POSTGRESQL:
        return psycopg.connect(
            host=spec.host,
            port=spec.port,
            dbname=spec.database,
            user=spec.username,
            password=spec.password,
            autocommit=True,
            connect_timeout=3,
        )
    return pymysql.connect(
        host=spec.host,
        port=spec.port,
        user=spec.username,
        password=spec.password,
        database=spec.database,
        autocommit=True,
        connect_timeout=3,
        charset="utf8mb4",
    )


def stop_server(db: RunningDb) -> None:
    """Remove the container."""
    docker_cli("rm", "--force", "--volumes", db.container)


def seed(db: RunningDb) -> None:
    """Create the shared test schema."""
    connection = connect_admin(db)
    try:
        if db.engine is Engine.POSTGRESQL:
            _seed_postgres(connection)
        else:
            _seed_mysql(connection)
    finally:
        connection.close()


def _item_rows() -> list[tuple[int, str, str | None, bytes | None]]:
    rows: list[tuple[int, str, str | None, bytes | None]] = []
    for number in range(1, ITEM_COUNT + 1):
        note = (
            "x" * BIG_TEXT_LENGTH if number == 7 else (None if number % 10 == 0 else f"n{number}")
        )
        blob = bytes(range(256)) + b"\x00" * (BINARY_LENGTH - 256) if number == 7 else None
        rows.append((number, f"item-{number:04d}", note, blob))
    return rows


def _seed_postgres(connection: Any) -> None:
    cursor = connection.cursor()
    cursor.execute("CREATE SCHEMA shop")
    cursor.execute(
        "CREATE TABLE shop.items (id integer PRIMARY KEY, "
        "name varchar(40) NOT NULL DEFAULT 'unnamed', note text, payload bytea, "
        "created timestamptz DEFAULT now(), price numeric(10,2), "
        "meta jsonb DEFAULT '{}'::jsonb)"
    )
    cursor.executemany(
        "INSERT INTO shop.items (id, name, note, payload) VALUES (%s, %s, %s, %s)", _item_rows()
    )
    cursor.execute("CREATE INDEX items_name_idx ON shop.items (name)")
    cursor.execute("CREATE UNIQUE INDEX items_lower_idx ON shop.items (lower(name))")
    cursor.execute("CREATE TABLE shop.composite (a integer, b integer, v text, PRIMARY KEY (a, b))")
    cursor.execute(
        "INSERT INTO shop.composite SELECT a, b, a || '-' || b FROM generate_series(1,5) a, "
        "generate_series(1,5) b"
    )
    cursor.execute(
        "CREATE TABLE shop.orders (id serial PRIMARY KEY, item_id integer NOT NULL "
        "REFERENCES shop.items(id) ON DELETE CASCADE ON UPDATE RESTRICT, qty integer)"
    )
    cursor.execute("INSERT INTO shop.orders (item_id, qty) VALUES (1, 2), (2, 3)")
    cursor.execute("CREATE TABLE shop.nopk (x integer, y text)")
    cursor.execute("INSERT INTO shop.nopk VALUES (2, 'b'), (1, 'a'), (3, 'c')")
    cursor.execute("CREATE VIEW shop.item_names AS SELECT id, name FROM shop.items")
    cursor.execute("ANALYZE")
    columns = ", ".join(f'"{c.replace(chr(34), chr(34) * 2)}" integer' for c in HOSTILE_COLUMNS_PG)
    hostile = HOSTILE_PG.replace('"', '""')
    cursor.execute(f'CREATE TABLE shop."{hostile}" (id integer PRIMARY KEY, {columns})')
    cursor.execute(f'INSERT INTO shop."{hostile}" VALUES (1, 10, 20, 30)')  # noqa: S608


def _seed_mysql(connection: Any) -> None:
    cursor = connection.cursor()
    cursor.execute("CREATE DATABASE shop")
    cursor.execute(
        "CREATE TABLE shop.items (id int PRIMARY KEY, name varchar(40) NOT NULL DEFAULT 'unnamed', "
        "note longtext, payload blob, created timestamp NULL DEFAULT CURRENT_TIMESTAMP, "
        "price decimal(10,2), INDEX items_name_idx (name)) ENGINE=InnoDB"
    )
    cursor.executemany(
        "INSERT INTO shop.items (id, name, note, payload) VALUES (%s, %s, %s, %s)", _item_rows()
    )
    cursor.execute(
        "CREATE TABLE shop.composite (a int, b int, v varchar(20), "
        "PRIMARY KEY (a, b)) ENGINE=InnoDB"
    )
    cursor.executemany(
        "INSERT INTO shop.composite VALUES (%s, %s, %s)",
        [(a, b, f"{a}-{b}") for a in range(1, 6) for b in range(1, 6)],
    )
    cursor.execute(
        "CREATE TABLE shop.orders (id int AUTO_INCREMENT PRIMARY KEY, "
        "item_id int NOT NULL, qty int, "
        "CONSTRAINT orders_item_fk FOREIGN KEY (item_id) REFERENCES shop.items(id) "
        "ON DELETE CASCADE ON UPDATE RESTRICT) ENGINE=InnoDB"
    )
    cursor.execute("INSERT INTO shop.orders (item_id, qty) VALUES (1, 2), (2, 3)")
    cursor.execute("CREATE TABLE shop.nopk (x int, y varchar(10)) ENGINE=InnoDB")
    cursor.execute("INSERT INTO shop.nopk VALUES (2, 'b'), (1, 'a'), (3, 'c')")
    cursor.execute("CREATE VIEW shop.item_names AS SELECT id, name FROM shop.items")
    cursor.execute("ANALYZE TABLE shop.items, shop.composite, shop.orders, shop.nopk")
    cursor.fetchall()
    columns = ", ".join(f"`{c.replace('`', '``')}` int" for c in HOSTILE_COLUMNS_MYSQL)
    hostile = HOSTILE_MYSQL.replace("`", "``")
    cursor.execute(f"CREATE TABLE shop.`{hostile}` (id int PRIMARY KEY, {columns})")
    cursor.execute(
        f"INSERT INTO shop.`{hostile}` VALUES (1, 10, 20, 30)"  # noqa: S608
    )
