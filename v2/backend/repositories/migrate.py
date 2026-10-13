"""Run database migrations: ``python -m backend.repositories.migrate``."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from backend.process import apply_shared_umask

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def build_config(database_url: str | None = None) -> Config:
    """Build an Alembic config from the installed package location (no ini file).

    Without ``database_url`` the migration environment reads it from ``Settings``.
    """
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    if database_url is not None:
        config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def upgrade_to_head(database_url: str | None = None) -> None:
    """Upgrade the database to the latest revision."""
    command.upgrade(build_config(database_url), "head")


if __name__ == "__main__":
    apply_shared_umask()
    upgrade_to_head()
