"""API token queries."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from backend.domain.paging import PageQuery
from backend.repositories.models import ApiToken
from backend.repositories.paging import contains, ordered

TOKEN_SORTS = {
    "created": ApiToken.created_at,
    "name": func.lower(ApiToken.name),
    "last_used": ApiToken.last_used_at,
}
TOKEN_STATES = ("active", "revoked", "expired")


class ApiTokenRepository:
    """Queries for ``ApiToken``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, token: ApiToken) -> ApiToken:
        """Persist ``token`` and assign its id."""
        self._session.add(token)
        self._session.flush()
        return token

    def get(self, token_id: int) -> ApiToken | None:
        """Return the token with ``token_id`` or ``None``."""
        return self._session.get(ApiToken, token_id)

    def get_many(self, token_ids: Sequence[int]) -> Sequence[ApiToken]:
        """Return the tokens with the given ids (one query)."""
        if not token_ids:
            return []
        return self._session.scalars(select(ApiToken).where(ApiToken.id.in_(token_ids))).all()

    def get_by_hash(self, token_hash: str) -> ApiToken | None:
        """Return the token with ``token_hash`` or ``None``."""
        return self._session.scalar(select(ApiToken).where(ApiToken.token_hash == token_hash))

    def list(self, *, created_by: int | None = None) -> Sequence[ApiToken]:
        """Return tokens, newest first; ``created_by`` limits them to one owner."""
        query = select(ApiToken).order_by(ApiToken.created_at.desc(), ApiToken.id.desc())
        if created_by is not None:
            query = query.where(ApiToken.created_by == created_by)
        return self._session.scalars(query).all()

    def search(
        self, query: PageQuery, *, state: str | None, now: datetime, owner_id: int | None = None
    ) -> tuple[Sequence[ApiToken], int]:
        """One page of tokens and the total number of matches.

        The search text matches the name and the visible prefix; ``state`` is ``active``,
        ``revoked`` or ``expired`` (``None`` = all). Sort keys: ``created`` (default, newest
        first), ``name`` and ``last_used``.
        """
        filters: list[ColumnElement[bool]] = []
        if owner_id is not None:
            filters.append(ApiToken.created_by == owner_id)
        if query.term is not None:
            filters.append(contains([ApiToken.name, ApiToken.token_prefix], query.term))
        if state == "revoked":
            filters.append(ApiToken.revoked_at.is_not(None))
        elif state == "expired":
            filters.append(ApiToken.revoked_at.is_(None))
            filters.append(ApiToken.expires_at <= now)
        elif state == "active":
            filters.append(ApiToken.revoked_at.is_(None))
            filters.append(ApiToken.expires_at.is_(None) | (ApiToken.expires_at > now))
        statement = ordered(
            select(ApiToken).where(*filters),
            query,
            TOKEN_SORTS,
            "created",
            descending=frozenset({"created", "last_used"}),
            tiebreak=ApiToken.id,
        )
        rows = self._session.scalars(statement.limit(query.limit).offset(query.offset)).all()
        total = self._session.scalar(select(func.count()).select_from(ApiToken).where(*filters))
        return rows, int(total or 0)
