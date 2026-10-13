"""Webhook delivery queries (idempotency log)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from backend.repositories.models import WebhookDelivery


class WebhookDeliveryRepository:
    """Queries for ``WebhookDelivery``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def exists(self, delivery_id: str) -> bool:
        """Whether a delivery with this GitHub delivery id was already handled."""
        query = select(WebhookDelivery.id).where(WebhookDelivery.delivery_id == delivery_id)
        return self._session.scalar(query) is not None

    def add(self, delivery: WebhookDelivery) -> WebhookDelivery:
        """Persist a delivery; the unique constraint rejects a replay."""
        self._session.add(delivery)
        self._session.flush()
        return delivery

    def delete_older_than(self, cutoff: datetime) -> int:
        """Forget deliveries received before ``cutoff``; return how many were removed."""
        result = self._session.execute(
            delete(WebhookDelivery).where(WebhookDelivery.received_at < cutoff)
        )
        return int(getattr(result, "rowcount", 0) or 0)

    def recent(self, limit: int) -> Sequence[WebhookDelivery]:
        """The newest deliveries first."""
        query = select(WebhookDelivery).order_by(WebhookDelivery.received_at.desc()).limit(limit)
        return self._session.scalars(query).all()

    def outcome_counts(self, since: datetime) -> dict[str, int]:
        """Deliveries received since ``since``, counted per outcome."""
        query = (
            select(WebhookDelivery.outcome, func.count())
            .where(WebhookDelivery.received_at >= since)
            .group_by(WebhookDelivery.outcome)
        )
        return {str(outcome): int(count) for outcome, count in self._session.execute(query)}
