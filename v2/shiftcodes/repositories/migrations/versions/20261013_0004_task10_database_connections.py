"""database connections for the inspector

Revision ID: 0004_task10
Revises: 0003
Create Date: 2026-10-13 15:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_task10"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "database_connections",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("environment_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=48), nullable=False),
        sa.Column(
            "engine",
            sa.Enum("postgresql", "mysql", "mariadb", name="engine", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("host", sa.String(length=253), nullable=False),
        sa.Column("port", sa.Integer(), nullable=False),
        sa.Column("database", sa.String(length=128), nullable=False),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("password_secret", sa.String(length=128), nullable=False),
        sa.Column(
            "tls_mode",
            sa.Enum("disable", "require", name="tlsmode", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_tested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_ok", sa.Boolean(), nullable=True),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_database_connections_environment_id_environments"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_database_connections")),
        sa.UniqueConstraint(
            "environment_id", "name", name=op.f("uq_database_connections_environment_id_name")
        ),
    )


def downgrade() -> None:
    op.drop_table("database_connections")
