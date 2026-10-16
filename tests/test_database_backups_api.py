"""Database backup routes share the application authorization policy and hide credentials."""

from __future__ import annotations

import secrets
from collections.abc import Mapping

import pytest

from backend.adapters.worker_client import WorkerClient
from backend.domain.actor import Actor
from backend.domain.database import DatabaseEngine, password_secret_name
from backend.domain.enums import EnvironmentName
from backend.repositories.models import DatabaseBackup, DatabaseConnection
from tests.access_support import Session
from tests.access_world import World

S3_MARKER = "fixture-s3-" + secrets.token_hex(8)
FIXTURE_LOGIN = "fixture_" + "reader"


def _connection(world: World, env_name: EnvironmentName, name: str) -> DatabaseConnection:
    with world.container.uow() as uow:
        environment = uow.environments.get_by_project_slug("demo", env_name)
        assert environment is not None
        return uow.database_connections.add(
            DatabaseConnection(
                environment_id=environment.id,
                name=name,
                engine=DatabaseEngine.POSTGRESQL,
                host="db",
                port=5432,
                database="app",
                username=FIXTURE_LOGIN,
                password_secret=password_secret_name(name),
            )
        )


def _backup(world: World, slug: str, env_name: EnvironmentName, name: str) -> DatabaseBackup:
    with world.container.uow() as uow:
        environment = uow.environments.get_by_project_slug(slug, env_name)
        assert environment is not None
        connection = uow.database_connections.add(
            DatabaseConnection(
                environment_id=environment.id,
                name=name,
                engine=DatabaseEngine.POSTGRESQL,
                host="db",
                port=5432,
                database="app",
                username=FIXTURE_LOGIN,
                password_secret=password_secret_name(name),
            )
        )
        return uow.database_backups.add(
            DatabaseBackup(
                connection_id=connection.id,
                environment_id=environment.id,
                trigger="manual",
                status="succeeded",
                started_at=world.container.clock(),
                finished_at=world.container.clock(),
                size_bytes=100,
                object_key=f"backups/{slug}/{env_name.value}/{name}/2026-10-16T030000.dump",
            )
        )


def _s3_settings() -> dict[str, object]:
    return {
        "access_key_id": "AKIAEXAMPLE",
        "secret_key": S3_MARKER,
        "bucket": "aic-consultation",
        "region": "eu-west-3",
        "prefix": "backups",
        "hour": 3,
        "enabled": False,
        "mysql_image": "mysql:8.4",
        "mariadb_image": "mariadb:11.4",
        "postgres_image": "postgres:17",
    }


def test_s3_backup_settings_are_admin_only_and_write_only(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, Mapping[str, object]]] = []

    def fake_call(
        _self: WorkerClient,
        method: str,
        _actor: Actor | None,
        params: Mapping[str, object],
        *,
        timeout: float | None = None,
    ) -> object:
        calls.append((method, params))
        return {"ok": False, "message": "S3 test failed safely"}

    monkeypatch.setattr(WorkerClient, "call", fake_call)
    paths = (
        "/api/integrations/s3-backups",
        "/api/integrations/s3-backups/test",
    )
    for username in ("viewer", "developer", "maintainer"):
        session = world[username]
        assert session.get(paths[0]).status_code == 403
        assert session.post(paths[1]).status_code == 403
        assert session.put("/api/integrations/s3-backups", _s3_settings()).status_code == 403

    admin = world.owner
    saved = admin.put("/api/integrations/s3-backups", _s3_settings())
    assert saved.status_code == 200
    assert S3_MARKER not in saved.text
    assert "secret_key" not in saved.json()
    assert calls[0][0] == "database_backups.set_s3_secret"
    assert calls[0][1] == {"secret_key": S3_MARKER}
    assert calls[1][0] == "database_backups.warm_images"

    fetched = admin.get(paths[0])
    assert fetched.status_code == 200
    assert isinstance(fetched.json()["secret_configured"], bool)
    assert "secret_key" not in fetched.json()
    assert S3_MARKER not in fetched.text
    tested = admin.post(paths[1])
    assert tested.status_code == 200
    assert tested.json() == {"ok": False, "message": "S3 test failed safely"}


def test_backup_routes_follow_read_run_and_project_visibility(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    _backup(world, "demo", EnvironmentName.DEVELOPMENT, "visible")
    _backup(world, "other", EnvironmentName.DEVELOPMENT, "hidden")

    visible = world["viewer"]
    assert visible.get("/api/projects/demo/environments/development/backups").status_code == 200
    rows = visible.get("/api/backups").json()
    assert {row["project"] for row in rows} == {"demo"}
    assert visible.get("/api/projects/other/environments/development/backups").status_code in {
        403,
        404,
    }
    assert world["nobody"].get(
        "/api/projects/demo/environments/development/backups"
    ).status_code in {
        403,
        404,
    }

    calls: list[str] = []

    def fake_call(
        _self: WorkerClient,
        method: str,
        _actor: Actor | None,
        _params: Mapping[str, object],
        *,
        timeout: float | None = None,
    ) -> object:
        calls.append(method)
        return {"started": True}

    monkeypatch.setattr(WorkerClient, "call", fake_call)
    environment = EnvironmentName.DEVELOPMENT
    for username in ("owner", "maintainer", "developer", "viewer"):
        connection = _connection(world, environment, f"run-{username}")
        session: Session = world.owner if username == "owner" else world[username]
        response = session.post(
            f"/api/projects/demo/environments/development/databases/{connection.name}/backups"
        )
        if username in {"owner", "maintainer"}:
            assert response.status_code == 202, response.text
            assert response.json()["status"] == "running"
        else:
            assert response.status_code == 403, response.text
    assert calls == ["database_backups.run", "database_backups.run"]
