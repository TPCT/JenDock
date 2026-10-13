"""Audit event queries."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from shiftcodes.repositories.models import AuditEvent


class AuditRepository:
    """Queries for ``AuditEvent``. Events are append-only."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, event: AuditEvent) -> AuditEvent:
        """Persist ``event`` and assign its id."""
        self._session.add(event)
        self._session.flush()
        return event

    def list(
        self,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
        action: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[AuditEvent]:
        """Return events newest first, optionally filtered, with pagination."""
        query = select(AuditEvent).order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
        if target_type is not None:
            query = query.where(AuditEvent.target_type == target_type)
        if target_id is not None:
            query = query.where(AuditEvent.target_id == target_id)
        if action is not None:
            query = query.where(AuditEvent.action == action)
        return self._session.scalars(query.limit(limit).offset(offset)).all()
