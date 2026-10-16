"""Persist database network and detection ownership.

Revision ID: 0013_database_connection_network
Revises: 0012_database_backups
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013_database_connection_network"
down_revision: str | None = "0012_database_backups"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("database_connections", recreate="never") as batch:
        batch.add_column(sa.Column("network", sa.String(length=255), nullable=True))
        batch.add_column(
            sa.Column("auto_detected", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT target_id, details FROM audit_events "
            "WHERE action = 'database.detect' AND target_type = 'environment'"
        )
    )
    detected: set[tuple[str, str]] = set()
    for environment_id, details in rows:
        if not str(environment_id).isdigit():
            continue
        try:
            payload = json.loads(details) if isinstance(details, str) else details
            names = payload.get("created", []) if isinstance(payload, dict) else []
            if isinstance(names, list):
                detected.update(
                    (str(environment_id), name) for name in names if isinstance(name, str)
                )
        except (TypeError, ValueError):
            continue
    for environment_id, name in detected:
        bind.execute(
            sa.text(
                "UPDATE database_connections SET auto_detected = true "
                "WHERE environment_id = :environment_id AND name = :name"
            ),
            {"environment_id": int(environment_id), "name": name},
        )


def downgrade() -> None:
    with op.batch_alter_table("database_connections", recreate="never") as batch:
        batch.drop_column("auto_detected")
        batch.drop_column("network")
