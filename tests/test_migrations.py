from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect

import backend.repositories
from backend.repositories.database import create_db_engine
from backend.repositories.migrate import MIGRATIONS_DIR, build_config, upgrade_to_head
from backend.repositories.models import Base


@pytest.fixture
def migrated_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"sqlite:///{tmp_path / 'fresh' / 'migrated.db'}"
    monkeypatch.setenv("JENDOCK_DATABASE_URL", url)
    upgrade_to_head()
    return url


def test_upgrade_head_matches_metadata(migrated_url: str) -> None:
    engine = create_db_engine(migrated_url)
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        assert compare_metadata(context, Base.metadata) == []
    engine.dispose()


def test_all_tables_created(migrated_url: str) -> None:
    engine = create_db_engine(migrated_url)
    tables = set(inspect(engine).get_table_names())
    engine.dispose()
    assert tables == set(Base.metadata.tables) | {"alembic_version"}


def test_downgrade_to_base_removes_tables(migrated_url: str) -> None:
    command.downgrade(build_config(), "base")
    engine = create_db_engine(migrated_url)
    assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
    engine.dispose()


def test_migrations_ship_inside_the_package() -> None:
    assert MIGRATIONS_DIR.parent == Path(backend.repositories.__file__).parent
    assert (MIGRATIONS_DIR / "script.py.mako").is_file()
    assert list((MIGRATIONS_DIR / "versions").glob("*_initial_schema.py"))


def test_upgrade_with_explicit_url_and_no_ini(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'explicit.db'}"
    upgrade_to_head(url)
    engine = create_db_engine(url)
    assert "projects" in inspect(engine).get_table_names()
    engine.dispose()


def test_run_execution_columns_exist_and_downgrade_removes_them(migrated_url: str) -> None:
    engine = create_db_engine(migrated_url)
    columns = {column["name"] for column in inspect(engine).get_columns("runs")}
    engine.dispose()
    assert {"cancel_requested_at", "message"} <= columns
    command.downgrade(build_config(), "0002")
    engine = create_db_engine(migrated_url)
    columns = {column["name"] for column in inspect(engine).get_columns("runs")}
    engine.dispose()
    assert not {"cancel_requested_at", "message"} & columns


def test_existing_users_become_owners_and_access_grants_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production upgrade: the one ``admin`` operator keeps full control as owner."""
    url = f"sqlite:///{tmp_path / 'old' / 'old.db'}"
    monkeypatch.setenv("JENDOCK_DATABASE_URL", url)
    upgrade_to_head()
    command.downgrade(build_config(), "0004_task10")
    engine = create_db_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO users (username, password_hash, is_active, created_at) "
            "VALUES ('admin', 'x', 1, '2026-10-13 12:00:00')"
        )
    engine.dispose()
    upgrade_to_head()
    engine = create_db_engine(url)
    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            "SELECT role, display_name, email, must_change_password FROM users"
        ).one()
    assert tuple(row) == ("owner", "", None, 0)
    assert "access_grants" in inspect(engine).get_table_names()
    engine.dispose()
