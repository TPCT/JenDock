"""Secret queries. Ciphertext only; decryption happens in the service layer."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from shiftcodes.repositories.models import Secret


class SecretRepository:
    """Queries for ``Secret``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, secret: Secret) -> Secret:
        """Persist ``secret`` and assign its id."""
        self._session.add(secret)
        self._session.flush()
        return secret

    def get(self, environment_id: int, name: str) -> Secret | None:
        """Return the secret ``name`` of an environment or ``None``."""
        query = select(Secret).where(Secret.environment_id == environment_id, Secret.name == name)
        return self._session.scalar(query)

    def list_for_environment(self, environment_id: int) -> Sequence[Secret]:
        """Return the secrets of an environment ordered by name."""
        query = select(Secret).where(Secret.environment_id == environment_id).order_by(Secret.name)
        return self._session.scalars(query).all()

    def get_many(self, environment_id: int, names: Sequence[str]) -> Sequence[Secret]:
        """Return the existing secrets among ``names``."""
        query = select(Secret).where(
            Secret.environment_id == environment_id, Secret.name.in_(names)
        )
        return self._session.scalars(query).all()

    def delete(self, secret: Secret) -> None:
        """Delete one secret."""
        self._session.delete(secret)
        self._session.flush()
