"""Conversion of driver values into bounded, JSON-safe cells.

A cell is a plain JSON scalar, or one of two small marker objects: ``{"truncated": true,
"text": ..., "limit": 4096}`` for long text and ``{"binary": true, "hex": ..., "bytes": N}``
for binary values (a hex preview of the first bytes and the total length).
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime, time
from decimal import Decimal

from shiftcodes.inspector.model import BINARY_PREVIEW_BYTES, CELL_MAX_BYTES

MAX_SAFE_INTEGER = 2**53 - 1
Cell = object


def truncate_text(text: str, limit: int = CELL_MAX_BYTES) -> tuple[str, bool]:
    """Cut ``text`` to at most ``limit`` UTF-8 bytes without splitting a character."""
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return text, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


def text_cell(text: str) -> Cell:
    """A text cell, or a ``truncated`` marker when it exceeds the cell limit."""
    cut, truncated = truncate_text(text)
    if truncated:
        return {"truncated": True, "text": cut, "limit": CELL_MAX_BYTES}
    return cut


def binary_cell(preview: bytes, total: int | None = None) -> Cell:
    """A hex preview marker; ``total`` is the full byte length when the preview was cut."""
    length = len(preview) if total is None else total
    return {
        "binary": True,
        "hex": preview[:BINARY_PREVIEW_BYTES].hex(),
        "bytes": length,
    }


def _number(value: int | float) -> Cell:
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    return value if abs(value) <= MAX_SAFE_INTEGER else str(value)


def _text_of(value: object) -> str:
    if isinstance(value, datetime | date | time):
        return value.isoformat()
    if isinstance(value, dict | list | tuple):
        return json.dumps(value, default=str, ensure_ascii=False)
    return str(value)


def to_cell(value: object) -> Cell:
    """Convert any driver value into a bounded JSON-safe cell."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return _number(value)
    if isinstance(value, bytes | bytearray | memoryview):
        raw = bytes(value)
        return binary_cell(raw[:BINARY_PREVIEW_BYTES], len(raw))
    return text_cell(value if isinstance(value, str) else _text_of(value))


def cell_size(cell: Cell) -> int:
    """Approximate serialized size of a cell in bytes (used for the result size budget)."""
    return len(json.dumps(cell, ensure_ascii=False).encode("utf-8")) + 2


def keyset_value(value: object) -> object | None:
    """A JSON value that identifies a primary key value for keyset paging, or ``None``.

    ``None`` means the value cannot round-trip through JSON reliably (binary, floats, ...);
    the caller then falls back to offset paging.
    """
    result: object | None = None
    if isinstance(value, bool):
        result = None
    elif isinstance(value, int):
        result = value if abs(value) <= MAX_SAFE_INTEGER else str(value)
    elif isinstance(value, str):
        result = value
    elif isinstance(value, datetime):
        result = value.isoformat(sep=" ")
    elif isinstance(value, date | time):
        result = value.isoformat()
    elif isinstance(value, Decimal) or type(value).__name__ == "UUID":
        result = str(value)
    return result
