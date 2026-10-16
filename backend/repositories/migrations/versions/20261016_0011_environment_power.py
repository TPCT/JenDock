"""Environment power state and development resource-limit origin.

Revision ID: 0011_environment_power
Revises: 0010_domain_adoption
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_environment_power"
down_revision: str | None = "0010_domain_adoption"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("environments") as batch:
        batch.add_column(sa.Column("auto_stop_hours", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("resource_limits") as batch:
        batch.add_column(
            sa.Column("origin", sa.String(length=16), nullable=False, server_default="user")
        )


def downgrade() -> None:
    with op.batch_alter_table("resource_limits") as batch:
        batch.drop_column("origin")
    with op.batch_alter_table("environments") as batch:
        batch.drop_column("started_at")
        batch.drop_column("auto_stop_hours")
