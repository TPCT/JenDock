"""Creating a backup archive.

The database is copied with SQLite's online backup API (a consistent snapshot while the API and
worker keep writing), never by copying the file. The archive holds that snapshot, the managed
compose overrides, optionally the per-run logs, and a manifest with a SHA-256 per file and the
Alembic revision. The secret key is not part of it; ``--include-key`` writes it to a separate
0600 file next to the archive.
"""

from __future__ import annotations

import io
import os
import sqlite3
import tarfile
import tempfile
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from shiftcodes.backup.manifest import (
    DATABASE_MEMBER,
    FORMAT_VERSION,
    KEY_SUFFIX,
    LOGS_PREFIX,
    MANIFEST_NAME,
    OVERRIDE_FILES,
    OVERRIDES_PREFIX,
    BackupError,
    FileEntry,
    HashingReader,
    Manifest,
    archive_name,
)
from shiftcodes.process import apply_shared_umask

DEPLOYMENTS_DIR = "deployments"
LOGS_DIR = "logs"
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700
KEY_WARNING = (
    "WARNING: the secret key was written to {path}. Anyone holding it and a backup can decrypt "
    "every stored secret. Keep it apart from the archive (offline, encrypted) and never commit "
    "or upload it with the archive."
)


@dataclass(frozen=True, slots=True)
class BackupSource:
    """Where the platform keeps its state."""

    data_dir: Path
    database_path: Path
    key_file: Path


@dataclass(frozen=True, slots=True)
class BackupResult:
    """What was written."""

    archive: Path
    key_copy: Path | None
    manifest: Manifest


def snapshot_database(database: Path, destination: Path) -> str:
    """Copy ``database`` to ``destination`` with the online backup API; return its Alembic
    revision. Raises ``BackupError`` when the database is missing or was never migrated."""
    if not database.is_file():
        raise BackupError(f"database not found: {database}")
    with (
        closing(sqlite3.connect(f"file:{database}?mode=ro", uri=True)) as source,
        closing(sqlite3.connect(destination)) as target,
    ):
        source.backup(target)
    with closing(sqlite3.connect(destination)) as copy:
        if copy.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise BackupError("the database snapshot failed its integrity check")
        try:
            row = copy.execute("SELECT version_num FROM alembic_version").fetchone()
        except sqlite3.DatabaseError as error:
            raise BackupError("the database has not been migrated (no alembic_version)") from error
    if row is None:
        raise BackupError("the database has no Alembic revision")
    return str(row[0])


def _override_files(data_dir: Path) -> Iterator[tuple[str, Path]]:
    root = data_dir / DEPLOYMENTS_DIR
    if not root.is_dir():
        return
    for project in sorted(p for p in root.iterdir() if p.is_dir()):
        for name in OVERRIDE_FILES:
            path = project / name
            if path.is_file() and not path.is_symlink():
                yield f"{OVERRIDES_PREFIX}{project.name}/{name}", path


def _log_files(data_dir: Path) -> Iterator[tuple[str, Path]]:
    root = data_dir / LOGS_DIR
    if not root.is_dir():
        return
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            yield f"{LOGS_PREFIX}{path.relative_to(root).as_posix()}", path


def _add_file(tar: tarfile.TarFile, member: str, path: Path, moment: float) -> FileEntry:
    with path.open("rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        info = tarfile.TarInfo(member)
        info.size, info.mtime, info.mode = size, int(moment), PRIVATE_FILE_MODE
        reader = HashingReader(handle, size)
        tar.addfile(info, reader)
    return FileEntry(member, reader.digest.hexdigest(), reader.size)


def _write_archive(
    target: Path, members: list[tuple[str, Path]], revision: str, now: datetime, logs: bool
) -> Manifest:
    stamp = now.timestamp()
    with tarfile.open(target, "w:gz") as tar:
        files = [_add_file(tar, member, path, stamp) for member, path in members]
        manifest = Manifest(FORMAT_VERSION, now.isoformat(), revision, logs, tuple(files))
        data = manifest.to_json().encode()
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size, info.mtime, info.mode = len(data), int(stamp), PRIVATE_FILE_MODE
        tar.addfile(info, io.BytesIO(data))
    return manifest


def _write_key(source: BackupSource, archive: Path) -> Path:
    if not source.key_file.is_file():
        raise BackupError(f"secret key file not found: {source.key_file}")
    destination = archive.with_name(archive.name.removesuffix(".tar.gz") + KEY_SUFFIX)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_FILE_MODE)
    try:
        os.fchmod(fd, PRIVATE_FILE_MODE)
        with os.fdopen(fd, "wb") as out:
            out.write(source.key_file.read_bytes())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return destination


def create_backup(
    source: BackupSource,
    target_dir: Path,
    *,
    include_logs: bool = False,
    include_key: bool = False,
    now: datetime | None = None,
) -> BackupResult:
    """Write a timestamped archive into ``target_dir`` (created 0700 when missing).

    The archive is built under a temporary name and renamed when complete, so a partial file
    never looks like a backup.
    """
    moment = now or datetime.now(UTC)
    apply_shared_umask()
    target_dir.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIR_MODE)
    final = target_dir / archive_name(moment)
    if final.exists():
        raise BackupError(f"{final} already exists")
    with tempfile.TemporaryDirectory(prefix=".snapshot-", dir=target_dir) as scratch:
        snapshot = Path(scratch) / "shiftcodes.db"
        revision = snapshot_database(source.database_path, snapshot)
        partial = target_dir / f".{final.name}.partial"
        try:
            members = [(DATABASE_MEMBER, snapshot), *_override_files(source.data_dir)]
            if include_logs:
                members.extend(_log_files(source.data_dir))
            manifest = _write_archive(partial, members, revision, moment, include_logs)
            partial.chmod(PRIVATE_FILE_MODE)
            partial.replace(final)
        finally:
            partial.unlink(missing_ok=True)
    key_copy = _write_key(source, final) if include_key else None
    return BackupResult(final, key_copy, manifest)
