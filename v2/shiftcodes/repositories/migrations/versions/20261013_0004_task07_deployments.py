"""deployment history

Revision ID: 0004_task07
Revises: 0003
Create Date: 2026-10-13 15:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_task07"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("environments", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("compose_env_files", sa.JSON(), nullable=False, server_default="[]")
        )
    op.create_table(
        "deployments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("environment_id", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "deploying",
                "succeeded",
                "failed",
                "rolled_back",
                "rollback_failed",
                name="deploymentstatus",
                native_enum=False,
                length=16,
                validate_strings=True,
            ),
            nullable=False,
        ),
        sa.Column("commit_sha", sa.String(length=40), nullable=False),
        sa.Column("services", sa.JSON(), nullable=False),
        sa.Column("message", sa.String(length=500), nullable=True),
        sa.Column("redeploy_of_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_deployments_environment_id_environments"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["redeploy_of_id"],
            ["deployments.id"],
            name=op.f("fk_deployments_redeploy_of_id_deployments"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["runs.id"],
            name=op.f("fk_deployments_run_id_runs"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_deployments")),
    )
    op.create_index(
        "ix_deployments_environment_id_created_at",
        "deployments",
        ["environment_id", "created_at"],
    )
    op.create_index("ix_deployments_run_id", "deployments", ["run_id"])
    op.create_index("ix_deployments_status", "deployments", ["status"])


def downgrade() -> None:
    op.drop_index("ix_deployments_status", table_name="deployments")
    op.drop_index("ix_deployments_run_id", table_name="deployments")
    op.drop_index("ix_deployments_environment_id_created_at", table_name="deployments")
    op.drop_table("deployments")
    with op.batch_alter_table("environments", schema=None) as batch_op:
        batch_op.drop_column("compose_env_files")
