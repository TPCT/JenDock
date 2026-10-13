"""The worker's daily backup schedule (off unless ``SHIFTCODES_BACKUP_ENABLED``)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from sqlalchemy.engine import make_url

from shiftcodes.backup.create import BackupSource, create_backup
from shiftcodes.backup.manifest import BackupError
from shiftcodes.backup.retention import prune_archives
from shiftcodes.config import Settings

logger = logging.getLogger(__name__)
Clock = Callable[[], datetime]


class Stopper(Protocol):
    """Something that can be waited on, like the worker's shutdown handler."""

    def wait(self, timeout: float) -> bool:
        """Sleep up to ``timeout`` seconds; True when shutdown was requested."""


def next_run(now: datetime, hour_utc: int) -> datetime:
    """The next time of day ``hour_utc``:00 UTC strictly after ``now``."""
    candidate = now.astimezone(UTC).replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    return candidate if candidate > now else candidate + timedelta(days=1)


def source_of(settings: Settings) -> BackupSource:
    """The state locations of ``settings`` (SQLite only)."""
    url = make_url(settings.database_url)
    if url.get_backend_name() != "sqlite" or not url.database:
        raise BackupError("backups support SQLite databases only")
    return BackupSource(settings.data_dir, Path(url.database), settings.secret_key_file)


class BackupScheduler:
    """Creates one backup per day and prunes old ones; never includes the secret key."""

    def __init__(self, settings: Settings, clock: Clock = lambda: datetime.now(UTC)) -> None:
        if settings.backup_dir is None:
            raise BackupError("SHIFTCODES_BACKUP_DIR is required when backups are enabled")
        self._settings = settings
        self._clock = clock

    def run_once(self) -> Path:
        """Create a backup now and prune; returns the archive."""
        settings = self._settings
        assert settings.backup_dir is not None  # noqa: S101 - checked in __init__
        result = create_backup(
            source_of(settings),
            settings.backup_dir,
            include_logs=settings.backup_include_logs,
            now=self._clock(),
        )
        prune_archives(settings.backup_dir, settings.backup_keep)
        return result.archive

    def run_forever(self, stop: Stopper) -> None:
        """Sleep until the configured hour, back up, repeat until shutdown. A failure is
        logged and retried the next day."""
        while True:
            now = self._clock()
            wait = (next_run(now, self._settings.backup_hour_utc) - now).total_seconds()
            if stop.wait(wait):
                return
            try:
                archive = self.run_once()
                logger.info("backup created", extra={"archive": archive.name})
            except Exception:
                logger.exception("scheduled backup failed")
