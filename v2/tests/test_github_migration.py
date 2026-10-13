"""Revision 0004_task06: the git transport column and the webhook delivery table."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from sqlalchemy import inspect, text

from shiftcodes.repositories.database import create_db_engine
from shiftcodes.repositories.migrate import build_config, upgrade_to_head


@pytest.fixture
def url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    database = f"sqlite:///{tmp_path / 'fresh' / 'migrated.db'}"
    monkeypatch.setenv("SHIFTCODES_DATABASE_URL", database)
    return database


def test_upgrade_adds_the_column_and_the_table(url: str) -> None:
    upgrade_to_head()
    engine = create_db_engine(url)
    inspector = inspect(engine)
    assert "git_transport" in {c["name"] for c in inspector.get_columns("projects")}
    assert {c["name"] for c in inspector.get_columns("webhook_deliveries")} == {
        "id",
        "delivery_id",
        "event",
        "repository",
        "outcome",
        "received_at",
    }
    unique = [u["column_names"] for u in inspector.get_unique_constraints("webhook_deliveries")]
    assert ["delivery_id"] in unique
    engine.dispose()


def test_existing_projects_keep_cloning_over_https(url: str) -> None:
    command.upgrade(build_config(), "0003")
    engine = create_db_engine(url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO projects (slug, name, repository, created_at) "
                "VALUES ('old', 'Old', 'octo/old', '2026-01-01 00:00:00')"
            )
        )
    engine.dispose()
    upgrade_to_head()
    engine = create_db_engine(url)
    with engine.connect() as connection:
        value = connection.execute(text("SELECT git_transport FROM projects")).scalar_one()
    assert value == "https"
    engine.dispose()


def test_downgrade_removes_both(url: str) -> None:
    upgrade_to_head()
    command.downgrade(build_config(), "0003")
    engine = create_db_engine(url)
    inspector = inspect(engine)
    assert "webhook_deliveries" not in inspector.get_table_names()
    assert "git_transport" not in {c["name"] for c in inspector.get_columns("projects")}
    engine.dispose()
