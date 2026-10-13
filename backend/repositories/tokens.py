"""API token queries."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.repositories.models import ApiToken


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
