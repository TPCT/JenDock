"""notifications: channels, rules and the delivery outbox

Revision ID: 0006_notifications
Revises: 0005_platform
Create Date: 2026-10-13 21:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_notifications"
down_revision: str | None = "0005_platform"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_channels",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("config_ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("hint", sa.String(length=8), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_test_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_ok", sa.Boolean(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_channels")),
        sa.UniqueConstraint("name", name=op.f("uq_notification_channels_name")),
    )
    op.create_table(
        "notification_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("environment_id", sa.Integer(), nullable=True),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("events", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_notification_rules_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_notification_rules_environment_id_environments"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["notification_channels.id"],
            name=op.f("fk_notification_rules_channel_id_notification_channels"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_rules")),
    )
    op.create_index(
        "ix_notification_rules_scope", "notification_rules", ["project_id", "environment_id"]
    )
    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("channel_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=True),
        sa.Column("event", sa.String(length=32), nullable=False),
        sa.Column("dedupe_key", sa.String(length=160), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("suppressed", sa.Integer(), nullable=False),
        sa.Column("is_test", sa.Boolean(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(length=300), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["channel_id"],
            ["notification_channels.id"],
            name=op.f("fk_notification_deliveries_channel_id_notification_channels"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_notification_deliveries_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_deliveries")),
    )
    op.create_index(
        "ix_notification_deliveries_due",
        "notification_deliveries",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_notification_deliveries_quiet",
        "notification_deliveries",
        ["channel_id", "dedupe_key", "created_at"],
    )
    op.create_index(
        "ix_notification_deliveries_project",
        "notification_deliveries",
        ["project_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_notification_deliveries_project", table_name="notification_deliveries")
    op.drop_index("ix_notification_deliveries_quiet", table_name="notification_deliveries")
    op.drop_index("ix_notification_deliveries_due", table_name="notification_deliveries")
    op.drop_table("notification_deliveries")
    op.drop_index("ix_notification_rules_scope", table_name="notification_rules")
    op.drop_table("notification_rules")
    op.drop_table("notification_channels")
