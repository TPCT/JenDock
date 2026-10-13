"""The audit log: search, filter, page and export (never shows secret values)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import AwareDatetime

from backend.api import authz
from backend.api.deps import Container, Target, require
from backend.api.principal import Principal
from backend.api.schemas.audit import AuditActionsResponse, AuditPageResponse
from backend.domain.access import Action
from backend.domain.audit import AuditFilter
from backend.domain.audit_text import known_actions
from backend.services.audit_log import (
    DEFAULT_PAGE_SIZE,
    EXPORT_MAX_ROWS,
    MAX_PAGE_SIZE,
    MAX_TEXT_LENGTH,
)

router = APIRouter(prefix="/api/audit", tags=["audit"])
AuditReader = Annotated[Principal, Depends(require(Action.AUDIT_READ, Target.NONE))]


def _criteria(  # noqa: PLR0913, PLR0917 - one parameter per documented filter
    q: Annotated[str | None, Query(max_length=MAX_TEXT_LENGTH)] = None,
    actor_type: Annotated[str | None, Query(pattern="^(user|token|system)$")] = None,
    actor_id: Annotated[str | None, Query(max_length=64)] = None,
    action: Annotated[str | None, Query(max_length=65)] = None,
    target_type: Annotated[str | None, Query(max_length=32)] = None,
    target_id: Annotated[str | None, Query(max_length=64)] = None,
    project: Annotated[str | None, Query(max_length=64)] = None,
    since: AwareDatetime | None = None,
    until: AwareDatetime | None = None,
) -> AuditFilter:
    return AuditFilter(
        text=q or None,
        actor_type=actor_type,
        actor_id=actor_id or None,
        action=action or None,
        target_type=target_type or None,
        target_id=target_id or None,
        project=project or None,
        since=since,
        until=until,
    )


Criteria = Annotated[AuditFilter, Depends(_criteria)]


@router.get("")
def search_audit(
    principal: AuditReader,
    container: Container,
    criteria: Criteria,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> AuditPageResponse:
    """Events newest first. Filters combine with AND; ``action`` ending in ``*`` is a prefix;
    ``project`` is a slug. Pass ``next_cursor`` as ``cursor`` for the following page."""
    scoped = authz.audit_filter(principal, criteria)
    page = container.audit_log_service().search(scoped, cursor=cursor, limit=limit)
    return AuditPageResponse.from_page(page)


@router.get("/actions")
def audit_actions(_principal: AuditReader) -> AuditActionsResponse:
    """The actions the log can contain (for the filter drop-down)."""
    return AuditActionsResponse(actions=known_actions())


@router.get("/export.csv")
def export_audit(
    principal: AuditReader, container: Container, criteria: Criteria
) -> StreamingResponse:
    """The matching events as CSV, streamed; at most the newest 10 000 rows."""
    service = container.audit_log_service()
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    rows = service.export_csv(authz.audit_filter(principal, criteria))
    return StreamingResponse(
        rows,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="jendock-audit-{stamp}.csv"',
            "X-Export-Max-Rows": str(EXPORT_MAX_ROWS),
            "Cache-Control": "no-store",
        },
    )
