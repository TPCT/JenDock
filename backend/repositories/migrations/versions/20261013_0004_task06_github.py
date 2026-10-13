"""github integration: project git transport and webhook delivery log

Revision ID: 0004_task06
Revises: 0003
Create Date: 2026-10-13 15:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_task06"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "git_transport",
                sa.Enum("ssh", "https", name="gittransport", native_enum=False, length=16),
                nullable=False,
                server_default="https",
            )
        )
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("delivery_id", sa.String(length=64), nullable=False),
        sa.Column("event", sa.String(length=32), nullable=False),
        sa.Column("repository", sa.String(length=160), nullable=True),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_webhook_deliveries")),
        sa.UniqueConstraint("delivery_id", name=op.f("uq_webhook_deliveries_delivery_id")),
    )
    with op.batch_alter_table("webhook_deliveries", schema=None) as batch_op:
        batch_op.create_index("ix_webhook_deliveries_received_at", ["received_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("webhook_deliveries", schema=None) as batch_op:
        batch_op.drop_index("ix_webhook_deliveries_received_at")
    op.drop_table("webhook_deliveries")
    with op.batch_alter_table("projects", schema=None) as batch_op:
        batch_op.drop_column("git_transport")
