"""api tokens and run trigger token

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-13 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("token_prefix", sa.String(length=16), nullable=False),
        sa.Column("token_hash", sa.String(length=128), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_api_tokens_created_by_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_api_tokens_token_hash")),
    )
    with op.batch_alter_table("api_tokens", schema=None) as batch_op:
        batch_op.create_index("ix_api_tokens_created_by", ["created_by"], unique=False)
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.add_column(sa.Column("triggered_by_token_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            op.f("fk_runs_triggered_by_token_id_api_tokens"),
            "api_tokens",
            ["triggered_by_token_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.drop_constraint(
            op.f("fk_runs_triggered_by_token_id_api_tokens"), type_="foreignkey"
        )
        batch_op.drop_column("triggered_by_token_id")
    with op.batch_alter_table("api_tokens", schema=None) as batch_op:
        batch_op.drop_index("ix_api_tokens_created_by")
    op.drop_table("api_tokens")
