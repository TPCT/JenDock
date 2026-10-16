"""Database image and environment credential parsing rules."""

from backend.domain.database import DatabaseEngine
from backend.domain.database_detection import parse_database_credentials


def test_matches_registry_image_and_parses_mysql_credentials() -> None:
    result = parse_database_credentials(
        "library/mysql:8.4",
        {"MYSQL_DATABASE": "app", "MYSQL_USER": "reader", "MYSQL_PASSWORD": "secret"},
    )
    assert result is not None and result.credentials is not None
    assert result.credentials.engine is DatabaseEngine.MYSQL
    assert result.credentials.database == "app"
    assert result.credentials.username == "reader"
    assert result.credentials.password == "secret"


def test_mariadb_accepts_aliases_and_root_fallback() -> None:
    result = parse_database_credentials(
        "mariadb:11", {"MYSQL_DATABASE": "app", "MYSQL_ROOT_PASSWORD": "root-secret"}
    )
    assert result is not None and result.credentials is not None
    assert result.credentials.engine is DatabaseEngine.MYSQL
    assert result.credentials.username == "root"
    assert result.credentials.password == "root-secret"


def test_named_mysql_user_without_password_falls_back_to_root() -> None:
    result = parse_database_credentials(
        "mysql:8",
        {
            "MYSQL_DATABASE": "app",
            "MYSQL_USER": "application",
            "MYSQL_ROOT_PASSWORD": "root-secret",
        },
    )
    assert result is not None and result.credentials is not None
    assert result.credentials.username == "root"
    assert result.credentials.password == "root-secret"


def test_root_only_password_flags_do_not_reject_a_normal_user() -> None:
    result = parse_database_credentials(
        "mariadb:11",
        {
            "MARIADB_DATABASE": "app",
            "MARIADB_USER": "reader",
            "MARIADB_PASSWORD": "reader-secret",
            "MARIADB_ALLOW_EMPTY_ROOT_PASSWORD": "yes",
        },
    )
    assert result is not None and result.credentials is not None
    assert result.credentials.username == "reader"


def test_postgresql_defaults_database_and_user() -> None:
    result = parse_database_credentials("bitnami/postgresql:16", {"POSTGRES_PASSWORD": "secret"})
    assert result is not None and result.credentials is not None
    assert result.credentials.engine is DatabaseEngine.POSTGRESQL
    assert result.credentials.database == "postgres"
    assert result.credentials.username == "postgres"


def test_missing_database_or_file_password_is_skipped_safely() -> None:
    no_database = parse_database_credentials("mysql:8", {"MYSQL_PASSWORD": "secret"})
    assert (
        no_database is not None
        and no_database.reason == "no database name in the container environment"
    )
    file_password = parse_database_credentials(
        "postgres:16", {"POSTGRES_DB": "app", "POSTGRES_PASSWORD_FILE": "/run/secret"}
    )
    assert file_password is not None and file_password.credentials is None


def test_non_database_image_is_ignored() -> None:
    assert parse_database_credentials("redis:7", {}) is None
