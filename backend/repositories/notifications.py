"""Notification queries: channels, rules and the delivery outbox."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import ColumnElement, delete, func, select
from sqlalchemy.orm import Session, undefer

from backend.domain.notifications import DeliveryStatus
from backend.domain.paging import PageQuery
from backend.repositories.notification_models import (
    NotificationChannel,
    NotificationDelivery,
    NotificationRule,
)
from backend.repositories.paging import contains, ordered

DELIVERY_SORTS = {
    "created": NotificationDelivery.id,
    "status": NotificationDelivery.status,
}


class NotificationRepository:
    """Queries for ``NotificationChannel``, ``NotificationRule`` and ``NotificationDelivery``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    # --- channels ---------------------------------------------------------------------------

    def add_channel(self, channel: NotificationChannel) -> NotificationChannel:
        """Persist ``channel`` and assign its id."""
        self._session.add(channel)
        self._session.flush()
        return channel

    def get_channel(self, channel_id: int) -> NotificationChannel | None:
        """The channel without its ciphertext (deferred), or ``None``."""
        return self._session.get(NotificationChannel, channel_id)

    def get_channel_with_config(self, channel_id: int) -> NotificationChannel | None:
        """The channel including the encrypted configuration (worker only)."""
        query = (
            select(NotificationChannel)
            .options(undefer(NotificationChannel.config_ciphertext))
            .where(NotificationChannel.id == channel_id)
        )
        return self._session.scalar(query)

    def find_channel(self, name: str) -> NotificationChannel | None:
        """The channel called ``name`` or ``None``."""
        return self._session.scalar(
            select(NotificationChannel).where(NotificationChannel.name == name)
        )

    def list_channels(self) -> Sequence[NotificationChannel]:
        """All channels by name."""
        return self._session.scalars(
            select(NotificationChannel).order_by(NotificationChannel.name)
        ).all()

    def delete_channel(self, channel: NotificationChannel) -> None:
        """Delete a channel; its rules and deliveries go with it."""
        self._session.delete(channel)
        self._session.flush()

    # --- rules ------------------------------------------------------------------------------

    @staticmethod
    def _scope(project_id: int | None, environment_id: int | None) -> list[ColumnElement[bool]]:
        project = (
            NotificationRule.project_id.is_(None)
            if project_id is None
            else NotificationRule.project_id == project_id
        )
        environment = (
            NotificationRule.environment_id.is_(None)
            if environment_id is None
            else NotificationRule.environment_id == environment_id
        )
        return [project, environment]

    def rules_in_scope(
        self, project_id: int | None, environment_id: int | None
    ) -> Sequence[NotificationRule]:
        """The rules of exactly one scope, by channel."""
        query = select(NotificationRule).where(*self._scope(project_id, environment_id))
        return self._session.scalars(query.order_by(NotificationRule.channel_id)).all()

    def replace_rules(
        self,
        project_id: int | None,
        environment_id: int | None,
        rules: Sequence[NotificationRule],
    ) -> None:
        """Replace all rules of one scope atomically."""
        self._session.execute(
            delete(NotificationRule).where(*self._scope(project_id, environment_id))
        )
        self._session.add_all(rules)
        self._session.flush()

    def effective_rules(
        self, project_id: int | None, environment_id: int | None
    ) -> Sequence[NotificationRule]:
        """The rules that decide for a resource: environment, else project, else default."""
        if project_id is not None and environment_id is not None:
            found = self.rules_in_scope(project_id, environment_id)
            if found:
                return found
        if project_id is not None:
            found = self.rules_in_scope(project_id, None)
            if found:
                return found
        return self.rules_in_scope(None, None)

    # --- deliveries -------------------------------------------------------------------------

    def add_delivery(self, delivery: NotificationDelivery) -> NotificationDelivery:
        """Persist an outbox row."""
        self._session.add(delivery)
        self._session.flush()
        return delivery

    def get_delivery(self, delivery_id: int) -> NotificationDelivery | None:
        """One outbox row or ``None``."""
        return self._session.get(NotificationDelivery, delivery_id)

    def recent_duplicate(
        self, channel_id: int, dedupe_key: str, since: datetime
    ) -> NotificationDelivery | None:
        """The newest real (non-test) delivery of the same resource created after ``since``."""
        query = (
            select(NotificationDelivery)
            .where(
                NotificationDelivery.channel_id == channel_id,
                NotificationDelivery.dedupe_key == dedupe_key,
                NotificationDelivery.created_at > since,
                NotificationDelivery.is_test.is_(False),
            )
            .order_by(NotificationDelivery.id.desc())
            .limit(1)
        )
        return self._session.scalar(query)

    def due(
        self, now: datetime, stale_before: datetime, limit: int
    ) -> Sequence[NotificationDelivery]:
        """Rows to send now: pending and due, or ``sending`` since before ``stale_before``
        (the worker died while delivering). Oldest first."""
        pending = (NotificationDelivery.status == DeliveryStatus.PENDING.value) & (
            NotificationDelivery.next_attempt_at <= now
        )
        stuck = (NotificationDelivery.status == DeliveryStatus.SENDING.value) & (
            NotificationDelivery.claimed_at < stale_before
        )
        query = (
            select(NotificationDelivery)
            .where(pending | stuck)
            .order_by(NotificationDelivery.id)
            .limit(limit)
        )
        return self._session.scalars(query).all()

    def page(
        self,
        query: PageQuery,
        *,
        project_id: int | None,
        channel_id: int | None,
        status: str | None,
    ) -> tuple[Sequence[NotificationDelivery], int]:
        """A page of the delivery history with the total count.

        The search text matches the title, event and last error. Sort keys: ``created``
        (default, newest first) and ``status``.
        """
        filters: list[ColumnElement[bool]] = []
        if project_id is not None:
            filters.append(NotificationDelivery.project_id == project_id)
        if channel_id is not None:
            filters.append(NotificationDelivery.channel_id == channel_id)
        if status is not None:
            filters.append(NotificationDelivery.status == status)
        if query.term is not None:
            columns = [
                NotificationDelivery.title,
                NotificationDelivery.event,
                NotificationDelivery.last_error,
            ]
            filters.append(contains(columns, query.term))
        total = self._session.scalar(
            select(func.count()).select_from(NotificationDelivery).where(*filters)
        )
        statement = ordered(
            select(NotificationDelivery).where(*filters),
            query,
            DELIVERY_SORTS,
            "created",
            descending=frozenset({"created"}),
            tiebreak=NotificationDelivery.id,
        )
        rows = self._session.scalars(statement.limit(query.limit).offset(query.offset)).all()
        return rows, int(total or 0)

    def prune_before(self, cutoff: datetime) -> int:
        """Delete finished deliveries created before ``cutoff``; returns how many."""
        finished = [DeliveryStatus.SENT.value, DeliveryStatus.FAILED.value]
        result = self._session.execute(
            delete(NotificationDelivery).where(
                NotificationDelivery.status.in_(finished), NotificationDelivery.created_at < cutoff
            )
        )
        return int(getattr(result, "rowcount", 0) or 0)
