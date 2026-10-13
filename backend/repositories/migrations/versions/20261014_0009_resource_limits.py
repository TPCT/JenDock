"""resource limits: CPU and memory limits per compose service of an environment

Adds ``resource_limits`` with one row per environment and compose service holding the CPU
quota (cores), the memory limit, the memory reservation (soft limit) and the CPU shares. The
platform renders the managed compose override from these rows on every deploy and recreate and
copies them to running containers with ``docker update`` when asked. No existing data changes.

Revision ID: 0009_resources
Revises: 0008_profile
Create Date: 2026-10-14 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009_resources"
down_revision: str | None = "0008_profile"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "resource_limits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("environment_id", sa.Integer(), nullable=False),
        sa.Column("service", sa.String(length=64), nullable=False),
        sa.Column("cpus", sa.Float(), nullable=True),
        sa.Column("memory_bytes", sa.BigInteger(), nullable=True),
        sa.Column("memory_reservation_bytes", sa.BigInteger(), nullable=True),
        sa.Column("cpu_shares", sa.Integer(), nullable=True),
        sa.Column("updated_by", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_resource_limits_environment_id_environments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.id"],
            name=op.f("fk_resource_limits_updated_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resource_limits")),
        sa.UniqueConstraint(
            "environment_id", "service", name=op.f("uq_resource_limits_environment_id_service")
        ),
    )


def downgrade() -> None:
    op.drop_table("resource_limits")
