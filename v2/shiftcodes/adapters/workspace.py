"""Per-run workspace directories with a disk bound."""

from __future__ import annotations

import logging
import os
import shutil
import stat
from pathlib import Path

from shiftcodes.adapters.errors import WorkspaceError

logger = logging.getLogger(__name__)
WORKSPACE_MODE = 0o750
DEFAULT_MAX_BYTES = 2 * 1024**3


class WorkspaceManager:
    """Owns ``<root>/<run_id>``: created 0750, removed after the run, bounded in size.

    ``host_root`` is the same directory as the Docker daemon sees it, for deployments where
    the worker runs in a container itself; by default both are the same path.
    """

    def __init__(
        self, root: Path, max_bytes: int = DEFAULT_MAX_BYTES, host_root: Path | None = None
    ) -> None:
        self._root = root
        self._max_bytes = max_bytes
        self._host_root = host_root or root

    def path(self, run_id: int) -> Path:
        """The workspace directory of a run (it may not exist)."""
        return self._root / str(run_id)

    def host_path(self, run_id: int) -> Path:
        """The path to give the Docker daemon when mounting the workspace."""
        return self._host_root / str(run_id)

    def create(self, run_id: int) -> Path:
        """Create an empty workspace, replacing any leftover of an earlier attempt."""
        self.remove(run_id)
        self._root.mkdir(parents=True, exist_ok=True, mode=WORKSPACE_MODE)
        path = self.path(run_id)
        path.mkdir(mode=WORKSPACE_MODE)
        path.chmod(WORKSPACE_MODE)
        return path

    def remove(self, run_id: int) -> None:
        """Delete the workspace; never raises (a failure is logged and the files remain)."""
        path = self.path(run_id)
        if not path.exists() and not path.is_symlink():
            return
        try:
            shutil.rmtree(path, onexc=_make_writable)
        except OSError:
            logger.warning("workspace could not be fully removed", extra={"run_id": run_id})

    def existing_run_ids(self) -> list[int]:
        """Run ids that have a workspace directory."""
        if not self._root.is_dir():
            return []
        return sorted(int(entry.name) for entry in self._root.iterdir() if entry.name.isdigit())

    def size(self, path: Path) -> int:
        """Bytes used by regular files below ``path`` (symlinks are not followed)."""
        total = 0
        for directory, _names, files in os.walk(path):
            for name in files:
                with_stat = Path(directory, name)
                try:
                    info = with_stat.lstat()
                except OSError:
                    continue
                if stat.S_ISREG(info.st_mode):
                    total += info.st_blocks * 512 if info.st_blocks else info.st_size
        return total

    def enforce_limit(self, path: Path) -> None:
        """Raise ``WorkspaceError`` when the workspace exceeds the configured bound."""
        used = self.size(path)
        if used > self._max_bytes:
            raise WorkspaceError(
                f"workspace is {used // 1024**2} MiB, over the limit of "
                f"{self._max_bytes // 1024**2} MiB"
            )


def _make_writable(function: object, path: str, _error: BaseException) -> None:
    """``rmtree`` handler: a read-only directory blocks deletion of its contents."""
    parent = Path(path).parent
    try:
        parent.chmod(parent.stat().st_mode | stat.S_IRWXU)
        Path(path).chmod(Path(path).lstat().st_mode | stat.S_IRWXU)
    except OSError:
        return
    if callable(function):
        try:
            function(path)
        except OSError:
            return
