"""Keeping the newest N backups of a directory."""

from __future__ import annotations

from pathlib import Path

from backend.backup.manifest import ARCHIVE_PREFIX, ARCHIVE_SUFFIX, KEY_SUFFIX


def list_archives(directory: Path) -> list[Path]:
    """Backup archives of ``directory``, newest first (the timestamp is in the name)."""
    if not directory.is_dir():
        return []
    found = [
        p
        for p in directory.iterdir()
        if p.is_file() and p.name.startswith(ARCHIVE_PREFIX) and p.name.endswith(ARCHIVE_SUFFIX)
    ]
    return sorted(found, key=lambda p: p.name, reverse=True)


def prune_archives(directory: Path, keep: int) -> list[Path]:
    """Delete all but the newest ``keep`` archives; returns what was deleted.

    Only files named like our archives are touched. A separately written key file next to an
    archive is never deleted automatically (scheduled backups never create one).
    """
    if keep < 1:
        raise ValueError("keep must be at least 1")
    removed = []
    for path in list_archives(directory)[keep:]:
        path.unlink(missing_ok=True)
        removed.append(path)
    return removed


def key_copy_of(archive: Path) -> Path:
    """Where ``--include-key`` put the key belonging to ``archive``."""
    return archive.with_name(archive.name.removesuffix(ARCHIVE_SUFFIX) + KEY_SUFFIX)
