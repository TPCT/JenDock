"""two-factor authentication, session client info, webhook branch

Revision ID: 0008_profile
Revises: 0007_access
Create Date: 2026-10-14 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_profile"
down_revision: str | None = "0007_access"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("totp_secret", sa.LargeBinary(), nullable=True))
        batch_op.add_column(sa.Column("totp_enabled_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("totp_last_step", sa.Integer(), nullable=True))
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("ip_address", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("user_agent", sa.String(length=255), nullable=True))
    with op.batch_alter_table("webhook_deliveries", schema=None) as batch_op:
        batch_op.add_column(sa.Column("branch", sa.String(length=200), nullable=True))
    op.create_table(
        "recovery_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("code_hash", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_recovery_codes_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_recovery_codes")),
        sa.UniqueConstraint("code_hash", name=op.f("uq_recovery_codes_code_hash")),
    )
    with op.batch_alter_table("recovery_codes", schema=None) as batch_op:
        batch_op.create_index("ix_recovery_codes_user_id", ["user_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("recovery_codes", schema=None) as batch_op:
        batch_op.drop_index("ix_recovery_codes_user_id")
    op.drop_table("recovery_codes")
    with op.batch_alter_table("webhook_deliveries", schema=None) as batch_op:
        batch_op.drop_column("branch")
    with op.batch_alter_table("sessions", schema=None) as batch_op:
        batch_op.drop_column("user_agent")
        batch_op.drop_column("ip_address")
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("totp_last_step")
        batch_op.drop_column("totp_enabled_at")
        batch_op.drop_column("totp_secret")
