"""Engine independent part of row paging: the page plan and the conversion of fetched rows."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from shiftcodes.inspector.cells import binary_cell, cell_size, keyset_value, text_cell, to_cell
from shiftcodes.inspector.model import (
    CELL_MAX_BYTES,
    CONSOLE_MAX_ROWS,
    MAX_PAGE_ROWS,
    RESULT_MAX_BYTES,
    ErrorCode,
    InspectorError,
    ResultColumn,
    RowPage,
    RowRequest,
)

MAX_OFFSET = 10_000_000
ORDERING_PRIMARY_KEY = "primary_key"
ORDERING_COLUMNS = "columns"
ORDERING_NONE = "none"


class Mode(StrEnum):
    """How a column is read from the server."""

    NATIVE = "native"
    TEXT = "text"
    BINARY = "binary"


@dataclass(frozen=True, slots=True)
class SelectColumn:
    """One column of the page query. ``TEXT`` columns are fetched as at most 4097 characters,
    ``BINARY`` columns as a short prefix plus their byte length (an extra trailing value).
    ``raw_type`` is the engine's own type token, used to choose the select expression."""

    name: str
    data_type: str
    mode: Mode
    raw_type: str = ""


@dataclass(frozen=True, slots=True)
class PagePlan:
    """What the page query selects and how rows are ordered."""

    select: list[SelectColumn]
    key_indexes: list[int]
    order_names: list[str]
    ordering: str

    @property
    def keyset_allowed(self) -> bool:
        """Keyset paging needs a total order, which only the primary key gives."""
        return self.ordering == ORDERING_PRIMARY_KEY


def check_request(request: RowRequest, plan: PagePlan) -> None:
    """Validate limits and the keyset values of a page request."""
    if not 1 <= request.limit <= MAX_PAGE_ROWS:
        raise InspectorError(ErrorCode.BAD_REQUEST, f"limit must be between 1 and {MAX_PAGE_ROWS}")
    if not 0 <= request.offset <= MAX_OFFSET:
        raise InspectorError(ErrorCode.BAD_REQUEST, f"offset must be between 0 and {MAX_OFFSET}")
    if request.after is not None:
        if not plan.keyset_allowed:
            raise InspectorError(ErrorCode.BAD_REQUEST, "this table has no primary key to page by")
        if len(request.after) != len(plan.key_indexes):
            raise InspectorError(ErrorCode.BAD_REQUEST, "cursor does not match the primary key")
        for value in request.after:
            if not isinstance(value, int | str) or isinstance(value, bool):
                raise InspectorError(
                    ErrorCode.BAD_REQUEST, "cursor values must be strings or integers"
                )


def build_page(plan: PagePlan, fetched: Sequence[Sequence[object]], request: RowRequest) -> RowPage:
    """Turn up to ``limit + 1`` fetched rows into a page with its continuation values."""
    has_more = len(fetched) > request.limit
    wanted = fetched[: request.limit]
    cells: list[list[object]] = []
    used = 0
    size_limited = False
    for raw in wanted:
        row = _row_cells(plan, raw)
        used += sum(cell_size(c) for c in row)
        if used > RESULT_MAX_BYTES and cells:
            size_limited = True
            break
        cells.append(row)
    returned = len(cells)
    more = has_more or size_limited
    last = wanted[returned - 1] if returned else None
    next_after = _next_after(plan, last) if more and plan.keyset_allowed else None
    return RowPage(
        columns=[ResultColumn(c.name, c.data_type) for c in plan.select],
        rows=cells,
        limit=request.limit,
        offset=request.offset,
        ordering=plan.ordering,
        order_by=plan.order_names,
        next_after=next_after,
        next_offset=request.offset + returned if more and request.after is None else None,
        size_limited=size_limited,
    )


def _row_cells(plan: PagePlan, raw: Sequence[object]) -> list[object]:
    extra = len(plan.select)
    cells: list[object] = []
    for index, column in enumerate(plan.select):
        value = raw[index]
        if column.mode is Mode.BINARY:
            total = raw[extra]
            extra += 1
            cells.append(_binary(value, total))
        elif column.mode is Mode.TEXT:
            cells.append(_text(value))
        else:
            cells.append(to_cell(value))
    return cells


def _binary(value: object, total: object) -> object:
    if value is None:
        return None
    preview = bytes(value) if isinstance(value, bytes | bytearray | memoryview) else b""
    length = total if isinstance(total, int) else len(preview)
    return binary_cell(preview, length)


def _text(value: object) -> object:
    if value is None:
        return None
    return text_cell(value if isinstance(value, str) else str(value))


def _next_after(plan: PagePlan, last: Sequence[object] | None) -> list[object] | None:
    if last is None or not plan.keyset_allowed:
        return None
    values: list[object] = []
    for index in plan.key_indexes:
        raw = last[index]
        if isinstance(raw, str) and len(raw) > CELL_MAX_BYTES:
            return None
        converted = keyset_value(raw)
        if converted is None:
            return None
        values.append(converted)
    return values


def bounded_console_rows(fetched: Sequence[Sequence[object]]) -> tuple[list[list[object]], bool]:
    """Convert console rows to cells, keeping at most ``CONSOLE_MAX_ROWS`` rows and the size
    budget; the flag tells whether the size budget cut the result."""
    rows: list[list[object]] = []
    used = 0
    for raw in fetched[:CONSOLE_MAX_ROWS]:
        row = [to_cell(v) for v in raw]
        used += sum(cell_size(c) for c in row)
        if used > RESULT_MAX_BYTES and rows:
            return rows, True
        rows.append(row)
    return rows, False
