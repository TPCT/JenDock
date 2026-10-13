"""ingress: domains, certificates and status checks

Revision ID: 0004_task11
Revises: 0004_task06
Create Date: 2026-10-13 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_task11"
down_revision: str | None = "0004_task06"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "domains",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("environment_id", sa.Integer(), nullable=False),
        sa.Column("hostname", sa.String(length=253), nullable=False),
        sa.Column("service", sa.String(length=63), nullable=False),
        sa.Column("container_port", sa.Integer(), nullable=False),
        sa.Column("published_port", sa.Integer(), nullable=False),
        sa.Column(
            "dns_record_type",
            sa.Enum("A", "AAAA", "CNAME", name="dnsrecordtype", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("dns_target", sa.String(length=255), nullable=True),
        sa.Column("dns_proxied", sa.Boolean(), nullable=False),
        sa.Column("tls_enabled", sa.Boolean(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("draft", "active", "error", name="domainstatus", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("status_message", sa.String(length=500), nullable=True),
        sa.Column("cloudflare_zone_id", sa.String(length=64), nullable=True),
        sa.Column("cloudflare_record_id", sa.String(length=64), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_verified_ok", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["environment_id"],
            ["environments.id"],
            name=op.f("fk_domains_environment_id_environments"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_domains")),
        sa.UniqueConstraint("hostname", name=op.f("uq_domains_hostname")),
    )
    op.create_index("ix_domains_environment_id", "domains", ["environment_id"])
    op.create_table(
        "domain_certificates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("domain_id", sa.Integer(), nullable=False),
        sa.Column("serial", sa.String(length=64), nullable=False),
        sa.Column("fingerprint_sha256", sa.String(length=64), nullable=False),
        sa.Column("challenge", sa.String(length=16), nullable=False),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("not_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domains.id"],
            name=op.f("fk_domain_certificates_domain_id_domains"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_domain_certificates")),
        sa.UniqueConstraint("domain_id", name=op.f("uq_domain_certificates_domain_id")),
    )
    op.create_table(
        "domain_checks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("domain_id", sa.Integer(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("results", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(
            ["domain_id"],
            ["domains.id"],
            name=op.f("fk_domain_checks_domain_id_domains"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_domain_checks")),
        sa.UniqueConstraint("domain_id", name=op.f("uq_domain_checks_domain_id")),
    )


def downgrade() -> None:
    op.drop_table("domain_checks")
    op.drop_table("domain_certificates")
    op.drop_index("ix_domains_environment_id", table_name="domains")
    op.drop_table("domains")
