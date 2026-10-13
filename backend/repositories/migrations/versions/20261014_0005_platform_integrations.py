"""platform secrets and settings (Cloudflare token, ingress defaults)

Revision ID: 0005_platform
Revises: 0004_task10
Create Date: 2026-10-14 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_platform"
down_revision: str | None = "0004_task10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "platform_secrets",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_platform_secrets")),
    )
    op.create_table(
        "platform_settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.String(length=255), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_platform_settings")),
    )


def downgrade() -> None:
    op.drop_table("platform_settings")
    op.drop_table("platform_secrets")
