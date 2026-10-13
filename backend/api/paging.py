"""List endpoints' shared request parameters and response envelope."""

from __future__ import annotations

from typing import Annotated, Any, Self

from fastapi import Depends, Query
from pydantic import BaseModel

from backend.domain.paging import (
    DEFAULT_PER_PAGE,
    MAX_PAGE,
    MAX_PER_PAGE,
    MAX_SEARCH_LENGTH,
    Page,
    PageQuery,
    SortOrder,
)


class PageParams:
    """``page``, ``per_page``, ``q``, ``sort`` and ``order`` query parameters.

    ``limit`` and ``offset`` are the older spelling kept for existing clients: ``limit`` is
    ``per_page`` and ``offset`` the number of rows to skip.
    """

    def __init__(  # noqa: PLR0913, PLR0917 - one parameter per query-string field
        self,
        page: Annotated[int, Query(ge=1, le=MAX_PAGE, description="1-based page number")] = 1,
        per_page: Annotated[int, Query(ge=1, le=MAX_PER_PAGE)] = DEFAULT_PER_PAGE,
        q: Annotated[str | None, Query(max_length=MAX_SEARCH_LENGTH, description="search")] = None,
        sort: Annotated[str | None, Query(max_length=32)] = None,
        order: Annotated[SortOrder | None, Query()] = None,
        limit: Annotated[int | None, Query(ge=1, le=MAX_PER_PAGE, deprecated=True)] = None,
        offset: Annotated[int | None, Query(ge=0, deprecated=True)] = None,
    ) -> None:
        skip = None
        if limit is not None:
            per_page, skip = limit, offset or 0
            page = skip // limit + 1
        self.query = PageQuery(page, per_page, q, sort, order, skip)


Paging = Annotated[PageParams, Depends()]


class PageResponse[T](BaseModel):
    """The envelope of every list: the items of one page and how to reach the others."""

    items: list[T]
    total: int
    page: int
    per_page: int
    pages: int

    @classmethod
    def of(cls, page: Page[Any], items: list[T]) -> Self:
        """The envelope for ``page`` with its items already converted to response models."""
        return cls(items=items, **cls._meta(page))

    @classmethod
    def _meta(cls, page: Page[Any]) -> dict[str, int]:
        return {
            "total": page.total,
            "page": page.page,
            "per_page": page.per_page,
            "pages": page.pages,
        }


class LegacyPageResponse[T](PageResponse[T]):
    """``PageResponse`` plus the ``limit``/``offset`` fields older clients read."""

    limit: int
    offset: int

    @classmethod
    def _meta(cls, page: Page[Any]) -> dict[str, int]:
        return {**super()._meta(page), "limit": page.per_page, "offset": page.offset}
