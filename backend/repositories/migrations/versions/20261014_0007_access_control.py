"""users get roles and profile fields; access grants

Existing users are the platform's operators, so they all become owners. The first upgrade on
production therefore turns the one ``admin`` user into the owner.

Revision ID: 0007_access
Revises: 0006_notifications
Create Date: 2026-10-14 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_access"
down_revision: str | None = "0006_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = sa.Enum("owner", "admin", "member", name="globalrole", native_enum=False, length=16)
ENVIRONMENT = sa.Enum(
    "development", "production", name="environmentname", native_enum=False, length=16
)
ACCESS_ROLE = sa.Enum(
    "viewer", "developer", "maintainer", name="accessrole", native_enum=False, length=16
)


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(sa.Column("role", ROLE, server_default="member", nullable=False))
        batch_op.add_column(
            sa.Column("display_name", sa.String(length=120), server_default="", nullable=False)
        )
        batch_op.add_column(sa.Column("email", sa.String(length=254), nullable=True))
        batch_op.add_column(
            sa.Column("must_change_password", sa.Boolean(), server_default="0", nullable=False)
        )
    op.execute("UPDATE users SET role = 'owner'")
    op.create_table(
        "access_grants",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("environment", ENVIRONMENT, nullable=True),
        sa.Column("service", sa.String(length=64), nullable=True),
        sa.Column("scope_key", sa.String(length=100), nullable=False),
        sa.Column("role", ACCESS_ROLE, nullable=True),
        sa.Column("permissions", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_access_grants_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name=op.f("fk_access_grants_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_access_grants_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_access_grants")),
        sa.UniqueConstraint(
            "user_id",
            "project_id",
            "scope_key",
            name=op.f("uq_access_grants_user_id_project_id_scope_key"),
        ),
    )
    with op.batch_alter_table("access_grants", schema=None) as batch_op:
        batch_op.create_index("ix_access_grants_project_id", ["project_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("access_grants", schema=None) as batch_op:
        batch_op.drop_index("ix_access_grants_project_id")
    op.drop_table("access_grants")
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("must_change_password")
        batch_op.drop_column("email")
        batch_op.drop_column("display_name")
        batch_op.drop_column("role")
