"""Audit log use cases."""

from __future__ import annotations

from collections.abc import Sequence

from backend.domain.actor import Actor
from backend.domain.audit import AuditDetails, AuditEntry, AuditTarget, validate_audit_details
from backend.domain.clock import Clock, utcnow
from backend.domain.errors import ValidationError
from backend.repositories.database import UnitOfWork
from backend.repositories.models import AuditEvent

MAX_PAGE_SIZE = 200


def add_audit_event(uow: UnitOfWork, entry: AuditEntry, clock: Clock = utcnow) -> AuditEvent:
    """Validate and stage an audit event inside the active unit of work.

    Other services use this so the event commits atomically with their change.
    """
    event = AuditEvent(
        occurred_at=clock(),
        actor_type=entry.actor.type,
        actor_id=entry.actor.id,
        action=entry.action,
        target_type=entry.target.type,
        target_id=entry.target.id,
        details=validate_audit_details(entry.details),
    )
    return uow.audit.add(event)


class AuditService:
    """Records and queries audit events."""

    def __init__(self, uow: UnitOfWork, clock: Clock = utcnow) -> None:
        self._uow = uow
        self._clock = clock

    def record(
        self,
        actor: Actor,
        action: str,
        target: AuditTarget,
        details: AuditDetails | None = None,
    ) -> AuditEvent:
        """Store an event; sensitive detail keys raise ``ValidationError``."""
        with self._uow:
            entry = AuditEntry(actor, action, target, details or {})
            return add_audit_event(self._uow, entry, self._clock)

    def list(
        self,
        *,
        target: AuditTarget | None = None,
        action: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[AuditEvent]:
        """Return events newest first, filtered by target and/or action."""
        if not 1 <= limit <= MAX_PAGE_SIZE or offset < 0:
            raise ValidationError(f"limit must be 1-{MAX_PAGE_SIZE} and offset >= 0")
        with self._uow:
            return self._uow.audit.list(
                target_type=target.type if target else None,
                target_id=target.id if target else None,
                action=action,
                limit=limit,
                offset=offset,
            )
