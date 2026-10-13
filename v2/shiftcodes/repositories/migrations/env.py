"""Alembic environment: reads the database URL from Settings and uses ORM metadata."""

from __future__ import annotations

from typing import Literal

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy import Connection

from shiftcodes.config import Settings
from shiftcodes.repositories.database import create_db_engine
from shiftcodes.repositories.models import Base, UtcDateTime

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    """Prefer an explicit ``sqlalchemy.url`` option, else the application settings."""
    return config.get_main_option("sqlalchemy.url") or Settings().database_url


def _render_item(type_: str, obj: object, autogen_context: AutogenContext) -> str | Literal[False]:
    """Render the app-level ``UtcDateTime`` as plain ``sa.DateTime(timezone=True)``."""
    if type_ == "type" and isinstance(obj, UtcDateTime):
        autogen_context.imports.add("import sqlalchemy as sa")
        return "sa.DateTime(timezone=True)"
    return False


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_item=_render_item,
        render_as_batch=connection.dialect.name == "sqlite",
    )


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a database connection."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
        render_item=_render_item,
        render_as_batch=_database_url().startswith("sqlite"),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database."""
    engine = create_db_engine(_database_url())
    with engine.connect() as connection:
        _configure(connection)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
