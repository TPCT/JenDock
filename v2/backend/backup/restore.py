"""Restoring a backup archive into an empty data directory.

Nothing is written before the whole archive has been verified (every member against the
checksum in the manifest, no unexpected members, a known Alembic revision). A restore refuses a
data directory or database that already has content, and removes what it wrote when it fails.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import tarfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from alembic.script import ScriptDirectory

from backend.backup.create import BackupSource
from backend.backup.manifest import (
    CHUNK,
    DATABASE_MEMBER,
    LOGS_PREFIX,
    MANIFEST_NAME,
    OVERRIDES_PREFIX,
    BackupError,
    Manifest,
    validate_member,
)
from backend.process import apply_shared_umask
from backend.repositories.migrate import build_config

FILE_MODE = 0o640
DATABASE_MODE = 0o660
KEY_MODE = 0o600


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """What a restore wrote."""

    manifest: Manifest
    files: int
    key_restored: bool


def known_revisions() -> tuple[set[str], str]:
    """Every revision of this code's migration chain, and its head."""
    script = ScriptDirectory.from_config(build_config())
    revisions = {rev.revision for rev in script.walk_revisions()}
    return revisions, str(script.get_current_head())


def check_revision(revision: str) -> None:
    """Refuse a backup made by a newer version of the platform than this code."""
    revisions, head = known_revisions()
    if revision not in revisions:
        raise BackupError(
            f"the backup is at Alembic revision {revision!r}, unknown to this version "
            f"(head {head}): restore with the version that made it, or a newer one"
        )


def _iter_members(archive: Path) -> tarfile.TarFile:
    try:
        return tarfile.open(archive, "r:gz")
    except (tarfile.TarError, OSError) as error:
        raise BackupError(f"cannot read the archive: {error}") from error


def verify_archive(archive: Path) -> Manifest:
    """Check structure and checksums of every member; return the manifest."""
    manifest: Manifest | None = None
    seen: dict[str, tuple[str, int]] = {}
    with _iter_members(archive) as tar:
        for info in tar:
            name = validate_member(info.name)
            if not info.isfile():
                raise BackupError(f"not a regular file in the archive: {name!r}")
            handle = tar.extractfile(info)
            if handle is None:
                raise BackupError(f"cannot read {name!r}")
            digest, size = hashlib.sha256(), 0
            while chunk := handle.read(CHUNK):
                digest.update(chunk)
                size += len(chunk)
                if name == MANIFEST_NAME and size > CHUNK:
                    raise BackupError("the manifest is too large")
            if name == MANIFEST_NAME:
                handle = tar.extractfile(info)
                manifest = Manifest.from_json(handle.read().decode() if handle else "")
            elif name in seen:
                raise BackupError(f"duplicate file in the archive: {name!r}")
            else:
                seen[name] = (digest.hexdigest(), size)
    if manifest is None:
        raise BackupError("the archive has no manifest")
    _compare(manifest, seen)
    return manifest


def _compare(manifest: Manifest, seen: dict[str, tuple[str, int]]) -> None:
    listed = {entry.path: entry for entry in manifest.files}
    if len(listed) != len(manifest.files):
        raise BackupError("the manifest lists a file twice")
    if DATABASE_MEMBER not in listed:
        raise BackupError("the manifest does not list the database")
    if set(listed) != set(seen):
        extra = sorted(set(seen) ^ set(listed))
        raise BackupError(f"the archive does not match its manifest: {extra[:3]}")
    for path, entry in listed.items():
        digest, size = seen[path]
        if digest != entry.sha256 or size != entry.size:
            raise BackupError(f"checksum mismatch for {path}: the archive is corrupt or altered")


def _is_nonempty(path: Path) -> bool:
    return path.exists() and (not path.is_dir() or any(path.iterdir()))


def _destination(member: str, source: BackupSource) -> Path:
    if member == DATABASE_MEMBER:
        return source.database_path
    if member.startswith(OVERRIDES_PREFIX):
        return source.data_dir / "deployments" / member.removeprefix(OVERRIDES_PREFIX)
    if member.startswith(LOGS_PREFIX):
        return source.data_dir / "logs" / member.removeprefix(LOGS_PREFIX)
    raise BackupError(f"unexpected file in the archive: {member!r}")


def _extract(archive: Path, source: BackupSource, written: list[Path]) -> int:
    count = 0
    with _iter_members(archive) as tar:
        for info in tar:
            if info.name == MANIFEST_NAME:
                continue
            target = _destination(info.name, source)
            target.parent.mkdir(parents=True, exist_ok=True)
            handle = tar.extractfile(info)
            if handle is None:
                raise BackupError(f"cannot read {info.name!r}")
            mode = DATABASE_MODE if info.name == DATABASE_MEMBER else FILE_MODE
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
            written.append(target)
            with os.fdopen(fd, "wb") as out:
                shutil.copyfileobj(handle, out, CHUNK)
            target.chmod(mode)
            count += 1
    return count


def _check_restored_database(path: Path, manifest: Manifest) -> None:
    with closing(sqlite3.connect(path)) as database:
        if database.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise BackupError("the restored database failed its integrity check")
        row = database.execute("SELECT version_num FROM alembic_version").fetchone()
    if row is None or str(row[0]) != manifest.alembic_revision:
        raise BackupError("the restored database does not match the revision in the manifest")


def _restore_key(key_file: Path, destination: Path) -> bool:
    if destination.exists():
        raise BackupError(f"{destination} already exists: not overwriting a secret key")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, KEY_MODE)
    with os.fdopen(fd, "wb") as out:
        out.write(key_file.read_bytes())
    destination.chmod(KEY_MODE)
    return True


def restore_backup(
    archive: Path, source: BackupSource, *, key_file: Path | None = None
) -> RestoreResult:
    """Verify ``archive`` and restore it into ``source``'s data directory and database path.

    Refuses when the data directory or the database already has content. After it, run the
    migrations (``make migrate``) when the backup is older than this code.
    """
    if not archive.is_file():
        raise BackupError(f"archive not found: {archive}")
    if _is_nonempty(source.data_dir):
        raise BackupError(f"refusing to restore: {source.data_dir} is not empty")
    if source.database_path.exists():
        raise BackupError(f"refusing to restore: {source.database_path} already exists")
    if key_file is not None and not key_file.is_file():
        raise BackupError(f"key file not found: {key_file}")
    manifest = verify_archive(archive)
    check_revision(manifest.alembic_revision)
    apply_shared_umask()
    source.data_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    try:
        count = _extract(archive, source, written)
        _check_restored_database(source.database_path, manifest)
        restored_key = _restore_key(key_file, source.key_file) if key_file else False
    except BaseException:
        for path in reversed(written):
            path.unlink(missing_ok=True)
        _remove_empty_dirs(source.data_dir)
        raise
    return RestoreResult(manifest, count, restored_key)


def _remove_empty_dirs(root: Path) -> None:
    for directory in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            continue
