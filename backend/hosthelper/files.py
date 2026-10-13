"""Atomic file replacement and a small all-or-nothing change set with rollback."""

from __future__ import annotations

import contextlib
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path

DIR_MODE = 0o755
PRIVATE_DIR_MODE = 0o750


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    """Write ``data`` to ``path`` so readers see the old or the new file, never a partial one.

    The temporary file lives next to the target, is not matched by ``*.conf`` includes, is
    created exclusively with the final mode and is synced before the rename.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}.{secrets.token_hex(6)}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temp.chmod(mode)
        temp.replace(path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()
        raise
    _sync_directory(path.parent)


def _sync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass(slots=True)
class _Snapshot:
    path: Path
    content: bytes | None
    mode: int


@dataclass(slots=True)
class ChangeSet:
    """Remembers each touched file so ``rollback`` restores exactly the previous state."""

    _snapshots: list[_Snapshot] = field(default_factory=list)

    def _remember(self, path: Path) -> None:
        if any(item.path == path for item in self._snapshots):
            return
        try:
            info = path.lstat()
            content: bytes | None = path.read_bytes()
            mode = info.st_mode & 0o777
        except FileNotFoundError:
            content, mode = None, 0o644
        self._snapshots.append(_Snapshot(path, content, mode))

    def write(self, path: Path, data: bytes, mode: int) -> None:
        """Replace ``path`` atomically, remembering its previous content."""
        self._remember(path)
        atomic_write(path, data, mode)

    def delete(self, path: Path) -> None:
        """Remove ``path`` (a missing file is fine), remembering its previous content."""
        self._remember(path)
        with contextlib.suppress(FileNotFoundError):
            path.unlink()

    def rollback(self) -> None:
        """Restore every remembered file in reverse order; errors on one file do not stop others."""
        for item in reversed(self._snapshots):
            with contextlib.suppress(OSError):
                if item.content is None:
                    item.path.unlink(missing_ok=True)
                else:
                    atomic_write(item.path, item.content, item.mode)
        self._snapshots.clear()
