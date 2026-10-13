"""Reading the audit log: search with cursor pagination, descriptions and a bounded CSV export."""

from __future__ import annotations

import base64
import binascii
import csv
import io
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime

from backend.domain.audit import AuditFilter
from backend.domain.audit_text import describe_audit
from backend.domain.errors import ValidationError
from backend.repositories.database import UnitOfWork
from backend.repositories.models import AuditEvent

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
EXPORT_MAX_ROWS = 10_000
EXPORT_BATCH = 500
MAX_TEXT_LENGTH = 100
CSV_HEADER = (
    "id",
    "time",
    "actor_type",
    "actor_id",
    "actor",
    "action",
    "description",
    "target_type",
    "target_id",
    "details",
)
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


@dataclass(frozen=True, slots=True)
class AuditEntryView:
    """One event with its resolved actor name and description."""

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


@dataclass(frozen=True, slots=True)
class AuditPage:
    """A page of events, newest first; ``next_cursor`` is ``None`` on the last page."""

    items: tuple[AuditEntryView, ...]
    next_cursor: str | None


def encode_cursor(event_id: int) -> str:
    """The opaque cursor that continues after ``event_id``."""
    return base64.urlsafe_b64encode(f"a:{event_id}".encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> int:
    """The event id inside a cursor; ``ValidationError`` for anything else."""
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode()
        prefix, _, number = raw.partition(":")
        if prefix != "a" or not number.isdigit():
            raise ValueError(raw)
        return int(number)
    except (ValueError, binascii.Error, UnicodeDecodeError) as error:
        raise ValidationError("invalid cursor") from error


def validate_filter(criteria: AuditFilter) -> AuditFilter:
    """Reject over-long search text and an empty or inverted date range."""
    if criteria.text is not None and len(criteria.text) > MAX_TEXT_LENGTH:
        raise ValidationError(f"search text must be at most {MAX_TEXT_LENGTH} characters")
    if criteria.since and criteria.until and criteria.since >= criteria.until:
        raise ValidationError("the start of the date range must be before its end")
    return criteria


class AuditLogService:
    """Searches the append-only audit log. It never changes events."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    def search(
        self, criteria: AuditFilter, *, cursor: str | None = None, limit: int = DEFAULT_PAGE_SIZE
    ) -> AuditPage:
        """One page of matching events, newest first."""
        if not 1 <= limit <= MAX_PAGE_SIZE:
            raise ValidationError(f"limit must be 1-{MAX_PAGE_SIZE}")
        before = decode_cursor(cursor) if cursor else None
        return self._page(validate_filter(criteria), before, limit)

    def export_csv(self, criteria: AuditFilter) -> Iterator[str]:
        """The matching events as CSV text chunks, at most ``EXPORT_MAX_ROWS`` newest ones.

        Rows are read in batches of ``EXPORT_BATCH`` (a short transaction each), so memory
        stays flat however long the log is.
        """
        validate_filter(criteria)  # eagerly: errors must surface before streaming starts
        return self._export(criteria)

    def _export(self, criteria: AuditFilter) -> Iterator[str]:
        yield _csv_line(CSV_HEADER)
        before: int | None = None
        remaining = EXPORT_MAX_ROWS
        while remaining > 0:
            page = self._page(criteria, before, min(EXPORT_BATCH, remaining))
            for item in page.items:
                yield _csv_line(_csv_row(item))
            remaining -= len(page.items)
            if page.next_cursor is None:
                return
            before = decode_cursor(page.next_cursor)

    def _page(self, criteria: AuditFilter, before: int | None, limit: int) -> AuditPage:
        with self._uow:
            rows = self._uow.audit.search(criteria, before_id=before, limit=limit + 1)
            more = len(rows) > limit
            rows = rows[:limit]
            names = self._actor_names(rows)
            items = tuple(_view(row, names) for row in rows)
        return AuditPage(items, encode_cursor(rows[-1].id) if more and rows else None)

    def _actor_names(self, rows: list[AuditEvent]) -> dict[tuple[str, str], str]:
        """Usernames and token names of the actors on a page, in one query per kind."""
        user_ids = _numeric_ids(rows, "user")
        token_ids = _numeric_ids(rows, "token")
        names: dict[tuple[str, str], str] = {}
        for user in self._uow.users.get_many(sorted(user_ids)):
            names[("user", str(user.id))] = user.display_name or user.username
        for token in self._uow.tokens.get_many(sorted(token_ids)):
            names[("token", str(token.id))] = f"token {token.name}"
        return names


def _numeric_ids(rows: list[AuditEvent], actor_type: str) -> set[int]:
    return {
        int(row.actor_id)
        for row in rows
        if row.actor_type == actor_type and row.actor_id is not None and row.actor_id.isdigit()
    }


def _view(row: AuditEvent, names: dict[tuple[str, str], str]) -> AuditEntryView:
    actor_id = row.actor_id
    fallback = actor_id or "system"
    if row.actor_type == "user":
        fallback = f"user {actor_id} (deleted)" if actor_id else "unknown user"
    elif row.actor_type == "token":
        fallback = f"token {actor_id} (deleted)" if actor_id else "unknown token"
    label = names.get((row.actor_type, actor_id or ""), fallback)
    return AuditEntryView(
        id=row.id,
        occurred_at=row.occurred_at,
        actor_type=row.actor_type.value,
        actor_id=actor_id,
        actor=label,
        action=row.action,
        description=describe_audit(row.action, row.details),
        target_type=row.target_type,
        target_id=row.target_id,
        details=row.details,
    )


def _safe_cell(value: object) -> str:
    """A CSV cell that spreadsheets will not execute as a formula."""
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(_FORMULA_PREFIXES) else text


def _csv_row(item: AuditEntryView) -> tuple[str, ...]:
    return tuple(
        _safe_cell(value)
        for value in (
            item.id,
            item.occurred_at.isoformat(),
            item.actor_type,
            item.actor_id,
            item.actor,
            item.action,
            item.description,
            item.target_type,
            item.target_id,
            json.dumps(item.details, sort_keys=True, default=str),
        )
    )


def _csv_line(cells: tuple[str, ...]) -> str:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\r\n").writerow(cells)
    return buffer.getvalue()
