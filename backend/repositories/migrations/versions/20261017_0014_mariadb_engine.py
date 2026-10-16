"""Correct auto-detected MariaDB connection engines from deployment image history.

Revision ID: 0014_mariadb_engine
Revises: 0013_database_connection_network
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014_mariadb_engine"
down_revision: str | None = "0013_database_connection_network"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_mariadb(image: object) -> bool:
    if not isinstance(image, str):
        return False
    name = image.strip().rsplit("/", 1)[-1].split("@", 1)[0].split(":", 1)[0]
    return name.lower() == "mariadb"


def upgrade() -> None:
    bind = op.get_bind()
    deployments = bind.execute(sa.text("SELECT environment_id, services FROM deployments"))
    environment_services: dict[int, set[str]] = {}
    for environment_id, raw_services in deployments:
        try:
            services = json.loads(raw_services) if isinstance(raw_services, str) else raw_services
        except (TypeError, ValueError):
            continue
        if not isinstance(services, list):
            continue
        for item in services:
            if not isinstance(item, dict) or not isinstance(item.get("service"), str):
                continue
            if _is_mariadb(item.get("new_image")) or _is_mariadb(item.get("previous_image")):
                environment_services.setdefault(int(environment_id), set()).add(item["service"])
    for environment_id, services in environment_services.items():
        for service in services:
            bind.execute(
                sa.text(
                    "UPDATE database_connections SET engine = 'mariadb' "
                    "WHERE environment_id = :environment_id AND host = :host "
                    "AND auto_detected = true AND engine = 'mysql'"
                ),
                {"environment_id": environment_id, "host": service},
            )


def downgrade() -> None:
    # The original engine cannot be reconstructed reliably.
    pass
