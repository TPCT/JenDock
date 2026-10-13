"""ORM models of notifications: channels, rules and the delivery outbox.

Kept apart from ``models`` (imported by it at the bottom so the metadata is complete).
Channel secrets (webhook URLs, SMTP credentials) live only in ``config_ciphertext``, written
and read by the worker; the API never selects that column.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Index, LargeBinary, String
from sqlalchemy.orm import Mapped, mapped_column

from backend.domain.clock import utcnow
from backend.repositories.models import Base, UtcDateTime


class NotificationChannel(Base):
    """A destination: a Slack webhook, a generic webhook or an SMTP mailbox."""

    __tablename__ = "notification_channels"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    kind: Mapped[str] = mapped_column(String(16))
    enabled: Mapped[bool] = mapped_column(default=True)
    config_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    hint: Mapped[str | None] = mapped_column(String(8), default=None)
    summary: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    last_test_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    last_test_ok: Mapped[bool | None] = mapped_column(default=None)


class NotificationRule(Base):
    """Which events of a scope go to a channel.

    Scope: both ids ``None`` is the global default; ``project_id`` alone is a project;
    both set is one environment. The most specific scope that has any rule decides.
    """

    __tablename__ = "notification_rules"
    __table_args__ = (Index("ix_notification_rules_scope", "project_id", "environment_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), default=None
    )
    environment_id: Mapped[int | None] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE"), default=None
    )
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("notification_channels.id", ondelete="CASCADE")
    )
    events: Mapped[list[str]] = mapped_column(JSON, default=list)
    enabled: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class NotificationDelivery(Base):
    """The outbox: one message for one channel, retried with backoff until sent or exhausted."""

    __tablename__ = "notification_deliveries"
    __table_args__ = (
        Index("ix_notification_deliveries_due", "status", "next_attempt_at"),
        Index("ix_notification_deliveries_quiet", "channel_id", "dedupe_key", "created_at"),
        Index("ix_notification_deliveries_project", "project_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    channel_id: Mapped[int] = mapped_column(
        ForeignKey("notification_channels.id", ondelete="CASCADE")
    )
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), default=None
    )
    event: Mapped[str] = mapped_column(String(32))
    dedupe_key: Mapped[str] = mapped_column(String(160))
    title: Mapped[str] = mapped_column(String(200))
    payload: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    suppressed: Mapped[int] = mapped_column(default=0)
    is_test: Mapped[bool] = mapped_column(default=False)
    next_attempt_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    claimed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    last_error: Mapped[str | None] = mapped_column(String(300), default=None)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    delivered_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
