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

    def get_by_hash(self, token_hash: str) -> ApiToken | None:
        """Return the token with ``token_hash`` or ``None``."""
        return self._session.scalar(select(ApiToken).where(ApiToken.token_hash == token_hash))

    def list(self) -> Sequence[ApiToken]:
        """Return all tokens, newest first."""
        query = select(ApiToken).order_by(ApiToken.created_at.desc(), ApiToken.id.desc())
        return self._session.scalars(query).all()
