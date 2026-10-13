"""The shared list model: a validated page request and a page of results. No I/O."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from backend.domain.errors import ValidationError

DEFAULT_PER_PAGE = 25
MAX_PER_PAGE = 100
MAX_PAGE = 1_000_000
MAX_SEARCH_LENGTH = 100


class SortOrder(StrEnum):
    """Sort direction."""

    ASC = "asc"
    DESC = "desc"


@dataclass(frozen=True, slots=True)
class PageQuery:
    """One page request: 1-based ``page``, ``per_page``, free-text ``q`` and an optional sort.

    ``sort`` names one of the sort keys a list offers (``None`` = the list's default order) and
    ``order`` overrides that key's natural direction. ``skip`` is the older row-offset spelling:
    it replaces ``(page - 1) * per_page`` when set.
    """

    page: int = 1
    per_page: int = DEFAULT_PER_PAGE
    q: str | None = None
    sort: str | None = None
    order: SortOrder | None = None
    skip: int | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.page <= MAX_PAGE:
            raise ValidationError(f"page must be 1-{MAX_PAGE}")
        if not 1 <= self.per_page <= MAX_PER_PAGE:
            raise ValidationError(f"per_page must be 1-{MAX_PER_PAGE}")
        if self.skip is not None and self.skip < 0:
            raise ValidationError("offset must not be negative")
        if self.q is not None and len(self.q) > MAX_SEARCH_LENGTH:
            raise ValidationError(f"q must have at most {MAX_SEARCH_LENGTH} characters")

    @property
    def limit(self) -> int:
        """Rows to fetch."""
        return self.per_page

    @property
    def offset(self) -> int:
        """Rows to skip."""
        return self.skip if self.skip is not None else (self.page - 1) * self.per_page

    @property
    def term(self) -> str | None:
        """The trimmed search text, ``None`` when empty."""
        text = (self.q or "").strip()
        return text or None

    def sort_key(self, allowed: Iterable[str]) -> str | None:
        """The requested sort key; an unknown key is a validation error."""
        names = tuple(allowed)
        if self.sort is not None and self.sort not in names:
            raise ValidationError(f"unknown sort: {self.sort} (use one of {', '.join(names)})")
        return self.sort


@dataclass(frozen=True, slots=True)
class Page[T]:
    """One page of results with the total number of matches."""

    items: tuple[T, ...]
    total: int
    page: int
    per_page: int
    offset: int

    @classmethod
    def of(cls, items: Iterable[T], total: int, query: PageQuery) -> Page[T]:
        """A page answering ``query``."""
        return cls(tuple(items), total, query.page, query.per_page, query.offset)

    @property
    def pages(self) -> int:
        """The number of pages (at least 1, so an empty list still has a first page)."""
        return max(1, -(-self.total // self.per_page))


def paginate[T](  # noqa: PLR0913 - the list's search, sorts and default are all required
    items: Sequence[T],
    query: PageQuery,
    *,
    text: Callable[[T], Iterable[str | None]],
    sorts: Mapping[str, Callable[[T], Any]],
    default_sort: str,
    descending: frozenset[str] = frozenset(),
) -> Page[T]:
    """Search, sort and slice an in-memory list (data that does not live in the database).

    ``text`` yields the searchable strings of an item; ``sorts`` maps sort keys to key functions;
    keys in ``descending`` sort newest/largest first unless ``order`` says otherwise.
    """
    term = query.term
    found = list(items)
    if term is not None:
        needle = term.lower()
        found = [
            item for item in found if any(needle in (part or "").lower() for part in text(item))
        ]
    key = query.sort_key(sorts) or default_sort
    reverse = key in descending
    if query.order is not None:
        reverse = query.order is SortOrder.DESC
    found.sort(key=sorts[key], reverse=reverse)
    return Page.of(found[query.offset : query.offset + query.limit], len(found), query)
