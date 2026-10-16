from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect

import backend.repositories
from backend.domain.actor import Actor
from backend.domain.audit import AuditEntry, AuditTarget
from backend.domain.database import DatabaseEngine, TlsMode
from backend.domain.enums import EnvironmentName
from backend.repositories.database import UnitOfWork, create_db_engine, create_session_factory
from backend.repositories.migrate import MIGRATIONS_DIR, build_config, upgrade_to_head
from backend.repositories.models import Base, DatabaseConnection
from backend.services.audit import add_audit_event
from backend.services.projects import ProjectService


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


def test_database_connection_network_migration_backfills_detected_names(
    migrated_url: str,
) -> None:
    engine = create_db_engine(migrated_url)
    uow = UnitOfWork(create_session_factory(engine))
    detail = ProjectService(uow).create("detect-backfill", "Detect", "org/detect")
    environment = next(
        item for item in detail.environments if item.name is EnvironmentName.DEVELOPMENT
    )
    with uow:
        for name in ("created-automatically", "added-manually"):
            uow.database_connections.add(
                DatabaseConnection(
                    environment_id=environment.id,
                    name=name,
                    engine=DatabaseEngine.POSTGRESQL,
                    host="db",
                    port=5432,
                    database="app",
                    username="reader",
                    password_secret=f"DBCONN_{name.upper().replace('-', '_')}_PASSWORD",
                    tls_mode=TlsMode.DISABLE,
                )
            )
    with uow:
        add_audit_event(
            uow,
            AuditEntry(
                Actor.system("migration-test"),
                "database.detect",
                AuditTarget("environment", str(environment.id)),
                {"created": ["created-automatically"]},
            ),
        )
    command.downgrade(build_config(), "0012_database_backups")
    upgrade_to_head(migrated_url)
    with uow:
        values = {
            item.name: item.auto_detected
            for item in uow.database_connections.list_for_environment(environment.id)
        }
    assert values == {"created-automatically": True, "added-manually": False}
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


def test_existing_domains_stay_managed_and_the_discovery_tables_come_and_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite:///{tmp_path / 'domains' / 'old.db'}"
    monkeypatch.setenv("JENDOCK_DATABASE_URL", url)
    upgrade_to_head()
    command.downgrade(build_config(), "0009_resources")
    engine = create_db_engine(url)
    assert "discovered_domains" not in inspect(engine).get_table_names()
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO projects (id, slug, name, repository, git_transport, created_at) "
            "VALUES (1, 'demo', 'Demo', 'octo/demo', 'https', '2026-10-13 12:00:00')"
        )
        connection.exec_driver_sql(
            "INSERT INTO environments (id, project_id, name, branch, auto_deploy, services, "
            "compose_files, compose_env_files, created_at, updated_at) VALUES (1, 1, "
            "'production', 'main', 0, '[]', '[]', '[]', "
            "'2026-10-13 12:00:00', '2026-10-13 12:00:00')"
        )
        connection.exec_driver_sql(
            "INSERT INTO domains (environment_id, hostname, service, container_port, "
            "published_port, dns_record_type, dns_proxied, tls_enabled, status, created_at, "
            "updated_at) VALUES (1, 'app.example.com', 'web', 8080, 20001, 'A', 0, 1, "
            "'active', '2026-10-13 12:00:00', '2026-10-13 12:00:00')"
        )
    engine.dispose()
    upgrade_to_head()
    engine = create_db_engine(url)
    with engine.connect() as connection:
        row = connection.exec_driver_sql("SELECT origin, adopted_at, vhost_file FROM domains").one()
    assert tuple(row) == ("managed", None, None)
    assert {"discovered_domains", "discovery_state"} <= set(inspect(engine).get_table_names())
    engine.dispose()


def test_environment_power_columns_upgrade_and_downgrade(migrated_url: str) -> None:
    engine = create_db_engine(migrated_url)
    environment_columns = {column["name"] for column in inspect(engine).get_columns("environments")}
    limit_columns = {column["name"] for column in inspect(engine).get_columns("resource_limits")}
    assert {"auto_stop_hours", "started_at"} <= environment_columns
    assert "origin" in limit_columns
    engine.dispose()

    command.downgrade(build_config(), "0010_domain_adoption")
    engine = create_db_engine(migrated_url)
    environment_columns = {column["name"] for column in inspect(engine).get_columns("environments")}
    limit_columns = {column["name"] for column in inspect(engine).get_columns("resource_limits")}
    assert not {"auto_stop_hours", "started_at"} & environment_columns
    assert "origin" not in limit_columns
    engine.dispose()
