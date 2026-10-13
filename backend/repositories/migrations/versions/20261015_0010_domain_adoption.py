"""domain adoption: existing domains recorded in Jendock, and the discovery snapshot

Adds ``domains.origin`` (``managed`` for every existing row, ``adopted`` for domains that existed
before Jendock), ``domains.adopted_at`` and ``domains.vhost_file``; and two tables written by the
worker's discovery: ``discovered_domains`` (one row per host name found on Cloudflare or in
Apache, with the suggested project) and ``discovery_state`` (what the last refresh could read).
No existing data changes.

Revision ID: 0010_domain_adoption
Revises: 0009_resources
Create Date: 2026-10-15 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_domain_adoption"
down_revision: str | None = "0009_resources"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ORIGIN = sa.Enum(
    "managed", "adopted", name="domainorigin", native_enum=False, length=16, validate_strings=True
)
ENVIRONMENT = sa.Enum(
    "development",
    "production",
    name="environmentname",
    native_enum=False,
    length=16,
    validate_strings=True,
)
CONFIDENCE = sa.Enum(
    "high", "medium", "low", name="confidence", native_enum=False, length=16, validate_strings=True
)


def upgrade() -> None:
    with op.batch_alter_table("domains") as batch:
        batch.add_column(sa.Column("origin", ORIGIN, nullable=False, server_default="managed"))
        batch.add_column(sa.Column("adopted_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("vhost_file", sa.String(length=255), nullable=True))
    op.create_table(
        "discovered_domains",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("hostname", sa.String(length=253), nullable=False),
        sa.Column("zone_name", sa.String(length=253), nullable=True),
        sa.Column("zone_id", sa.String(length=64), nullable=True),
        sa.Column("record_id", sa.String(length=64), nullable=True),
        sa.Column("record_type", sa.String(length=8), nullable=True),
        sa.Column("record_content", sa.String(length=255), nullable=True),
        sa.Column("proxied", sa.Boolean(), nullable=False),
        sa.Column("points_to_server", sa.Boolean(), nullable=True),
        sa.Column("vhost_file", sa.String(length=255), nullable=True),
        sa.Column("vhost_managed", sa.Boolean(), nullable=False),
        sa.Column("upstream_host", sa.String(length=255), nullable=True),
        sa.Column("upstream_port", sa.Integer(), nullable=True),
        sa.Column("vhost_ssl", sa.Boolean(), nullable=True),
        sa.Column("suggested_project", sa.String(length=64), nullable=True),
        sa.Column("suggested_environment", ENVIRONMENT, nullable=True),
        sa.Column("suggested_service", sa.String(length=63), nullable=True),
        sa.Column("suggested_container_port", sa.Integer(), nullable=True),
        sa.Column("suggested_published_port", sa.Integer(), nullable=True),
        sa.Column("suggestion_confidence", CONFIDENCE, nullable=True),
        sa.Column("suggestion_reason", sa.String(length=300), nullable=True),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discovered_domains")),
        sa.UniqueConstraint("hostname", name=op.f("uq_discovered_domains_hostname")),
    )
    op.create_table(
        "discovery_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cloudflare", sa.String(length=16), nullable=False),
        sa.Column("cloudflare_message", sa.String(length=300), nullable=True),
        sa.Column("helper", sa.String(length=16), nullable=False),
        sa.Column("helper_message", sa.String(length=300), nullable=True),
        sa.Column("server_ip", sa.String(length=64), nullable=True),
        sa.Column("ip_source", sa.String(length=16), nullable=False),
        sa.Column("zones", sa.JSON(), nullable=False),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_discovery_state")),
    )


def downgrade() -> None:
    op.drop_table("discovery_state")
    op.drop_table("discovered_domains")
    with op.batch_alter_table("domains") as batch:
        batch.drop_column("vhost_file")
        batch.drop_column("adopted_at")
        batch.drop_column("origin")
