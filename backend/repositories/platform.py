"""Platform-level secrets and settings (not tied to a project or environment)."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.repositories.models import PlatformSecret, PlatformSetting


class PlatformRepository:
    """Queries for ``PlatformSecret`` and ``PlatformSetting``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_secret(self, name: str) -> PlatformSecret | None:
        """The platform secret ``name`` (with its ciphertext) or ``None``."""
        return self._session.get(PlatformSecret, name)

    def secret_metadata(self, name: str) -> tuple[str, object] | None:
        """``(name, updated_at)`` without loading the ciphertext; ``None`` when absent."""
        query = select(PlatformSecret.name, PlatformSecret.updated_at).where(
            PlatformSecret.name == name
        )
        row = self._session.execute(query).first()
        return None if row is None else (row[0], row[1])

    def add_secret(self, secret: PlatformSecret) -> PlatformSecret:
        """Persist a new platform secret."""
        self._session.add(secret)
        self._session.flush()
        return secret

    def delete_secret(self, secret: PlatformSecret) -> None:
        """Delete a platform secret."""
        self._session.delete(secret)
        self._session.flush()

    def settings(self) -> Sequence[PlatformSetting]:
        """Every stored setting."""
        return self._session.scalars(select(PlatformSetting)).all()

    def get_setting(self, key: str) -> PlatformSetting | None:
        """One stored setting."""
        return self._session.get(PlatformSetting, key)

    def add_setting(self, setting: PlatformSetting) -> PlatformSetting:
        """Persist a new setting."""
        self._session.add(setting)
        self._session.flush()
        return setting

    def delete_setting(self, setting: PlatformSetting) -> None:
        """Remove a stored setting (the environment default applies again)."""
        self._session.delete(setting)
        self._session.flush()
