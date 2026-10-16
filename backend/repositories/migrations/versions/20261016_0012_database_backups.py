"""Daily database backups to S3.

Revision ID: 0012_database_backups
Revises: 0011_environment_power
Create Date: 2026-10-16 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012_database_backups"
down_revision: str | None = "0011_environment_power"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # This column is a simple ADD COLUMN on both supported databases. Avoid
    # SQLite's batch table rebuild: environments has several live FK dependents.
    with op.batch_alter_table("environments", recreate="never") as batch:
        batch.add_column(
            sa.Column("include_in_backups", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    op.create_table(
        "database_backups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("connection_id", sa.Integer(), nullable=False),
        sa.Column("environment_id", sa.Integer(), nullable=False),
        sa.Column("trigger", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("object_key", sa.String(length=1024), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["database_connections.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["environment_id"], ["environments.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_database_backups")),
    )
    op.create_index(
        "ix_database_backups_connection_id_started_at",
        "database_backups",
        ["connection_id", "started_at"],
    )
    op.create_index("ix_database_backups_status", "database_backups", ["status"])


def downgrade() -> None:
    op.drop_index("ix_database_backups_status", table_name="database_backups")
    op.drop_index("ix_database_backups_connection_id_started_at", table_name="database_backups")
    op.drop_table("database_backups")
    with op.batch_alter_table("environments", recreate="never") as batch:
        batch.drop_column("include_in_backups")
