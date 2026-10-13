"""Audit log schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from backend.services.audit_log import AuditEntryView, AuditPage


class AuditEventResponse(BaseModel):
    """One audit event with its resolved actor and a human-readable description."""

    id: int
    occurred_at: datetime
    actor_type: str
    actor_id: str | None
    actor: str
    action: str
    description: str
    target_type: str
    target_id: str | None
    details: dict[str, object]

    @classmethod
    def from_view(cls, view: AuditEntryView) -> AuditEventResponse:
        """Build from the service view."""
        return cls(
            id=view.id,
            occurred_at=view.occurred_at,
            actor_type=view.actor_type,
            actor_id=view.actor_id,
            actor=view.actor,
            action=view.action,
            description=view.description,
            target_type=view.target_type,
            target_id=view.target_id,
            details=view.details,
        )


class AuditPageResponse(BaseModel):
    """A page of events, newest first; send ``next_cursor`` back to get the following page."""

    items: list[AuditEventResponse]
    next_cursor: str | None

    @classmethod
    def from_page(cls, page: AuditPage) -> AuditPageResponse:
        """Build from the service page."""
        return cls(
            items=[AuditEventResponse.from_view(item) for item in page.items],
            next_cursor=page.next_cursor,
        )


class AuditActionsResponse(BaseModel):
    """Known action names, for the filter drop-down."""

    actions: list[str]
