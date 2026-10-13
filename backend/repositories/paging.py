"""SQL building blocks for the shared list model: search and sort."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import ColumnElement, Select, SQLColumnExpression, String, cast, or_

from backend.domain.paging import PageQuery, SortOrder

ESCAPE = "\\"


def contains(columns: Sequence[SQLColumnExpression[Any]], term: str) -> ColumnElement[bool]:
    """Case-insensitive "contains ``term``" over any of ``columns``; wildcards in the term are
    literal (``%`` and ``_`` do not widen the match)."""
    escaped = term.replace(ESCAPE, ESCAPE * 2).replace("%", ESCAPE + "%").replace("_", ESCAPE + "_")
    pattern = f"%{escaped}%"
    return or_(*(cast(column, String).ilike(pattern, escape=ESCAPE) for column in columns))


def ordered[S: Select[*tuple[Any, ...]]](  # noqa: PLR0913 - the list's sort configuration
    statement: S,
    query: PageQuery,
    sorts: Mapping[str, SQLColumnExpression[Any]],
    default: str,
    *,
    descending: frozenset[str] = frozenset(),
    tiebreak: SQLColumnExpression[Any] | None = None,
) -> S:
    """``statement`` ordered by the requested sort key (else ``default``).

    Keys in ``descending`` default to newest/largest first; ``order`` overrides. ``tiebreak``
    (a unique column) keeps pages stable when the sort column has equal values.
    """
    key = query.sort_key(sorts) or default
    column = sorts[key]
    desc = key in descending
    if query.order is not None:
        desc = query.order is SortOrder.DESC
    clauses = [column.desc() if desc else column.asc()]
    if tiebreak is not None:
        clauses.append(tiebreak.desc() if desc else tiebreak.asc())
    return statement.order_by(*clauses)
