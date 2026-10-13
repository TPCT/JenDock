"""The manifest of a platform backup and the rules for member names."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import PurePosixPath
from typing import BinaryIO

FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
DATABASE_MEMBER = "db/shiftcodes.db"
OVERRIDES_PREFIX = "overrides/"
LOGS_PREFIX = "logs/"
ARCHIVE_PREFIX = "shiftcodes-backup-"
ARCHIVE_SUFFIX = ".tar.gz"
KEY_SUFFIX = ".secret.key"
TIMESTAMP_FORMAT = "%Y%m%dT%H%M%SZ"
OVERRIDE_FILES = ("override.yml", "override.yml.prev")
_PROJECT_DIR = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
CHUNK = 1024 * 1024


class BackupError(Exception):
    """A backup or restore cannot proceed; the message is shown to the operator."""


@dataclass(frozen=True, slots=True)
class FileEntry:
    """One archived file with its checksum."""

    path: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class Manifest:
    """What an archive holds. The secret key is never part of an archive."""

    format_version: int
    created_at: str
    alembic_revision: str
    includes_logs: bool
    files: tuple[FileEntry, ...]

    def to_json(self) -> str:
        """Serialize (stable key order)."""
        return json.dumps(
            {
                "format_version": self.format_version,
                "created_at": self.created_at,
                "alembic_revision": self.alembic_revision,
                "includes_logs": self.includes_logs,
                "includes_secret_key": False,
                "files": [{"path": f.path, "sha256": f.sha256, "size": f.size} for f in self.files],
            },
            indent=2,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, text: str) -> Manifest:
        """Parse a manifest; ``BackupError`` when it is malformed or from a newer format."""
        try:
            data = json.loads(text)
            manifest = cls(
                format_version=int(data["format_version"]),
                created_at=str(data["created_at"]),
                alembic_revision=str(data["alembic_revision"]),
                includes_logs=bool(data["includes_logs"]),
                files=tuple(
                    FileEntry(str(f["path"]), str(f["sha256"]), int(f["size"]))
                    for f in data["files"]
                ),
            )
        except (ValueError, KeyError, TypeError) as error:
            raise BackupError(f"the manifest is malformed: {error}") from error
        if manifest.format_version != FORMAT_VERSION:
            raise BackupError(f"unsupported backup format version {manifest.format_version}")
        return manifest


def archive_name(moment: datetime) -> str:
    """``shiftcodes-backup-<UTC timestamp>.tar.gz``."""
    return f"{ARCHIVE_PREFIX}{moment.strftime(TIMESTAMP_FORMAT)}{ARCHIVE_SUFFIX}"


def validate_member(name: str) -> str:
    """Return ``name`` when it is a safe, expected member path; ``BackupError`` otherwise."""
    path = PurePosixPath(name)
    if name != str(path) or path.is_absolute() or ".." in path.parts or "\0" in name:
        raise BackupError(f"unsafe path in the archive: {name!r}")
    if name in (MANIFEST_NAME, DATABASE_MEMBER) or name.startswith(LOGS_PREFIX):
        return name
    if name.startswith(OVERRIDES_PREFIX):
        parts = path.parts
        if len(parts) == 3 and _PROJECT_DIR.fullmatch(parts[1]) and parts[2] in OVERRIDE_FILES:
            return name
    raise BackupError(f"unexpected file in the archive: {name!r}")


class HashingReader:
    """Reads at most ``limit`` bytes of a file and hashes exactly what it returned."""

    def __init__(self, source: BinaryIO, limit: int) -> None:
        self._source = source
        self._left = limit
        self.digest = hashlib.sha256()
        self.size = 0

    def read(self, size: int = -1) -> bytes:
        """Return the next chunk, never more than the declared length."""
        want = self._left if size < 0 else min(size, self._left)
        data = self._source.read(want)
        self._left -= len(data)
        self.size += len(data)
        self.digest.update(data)
        return data
