"""The managed Compose override of each environment.

``<root>/<compose project>/override.yml`` sets ``image`` for the services the platform has
deployed. The deployer always appends it to the environment's compose files, so a later
restart or recreate of a service keeps the image that was deployed. The content is JSON (valid
YAML), written atomically; the version it replaces is kept as ``override.yml.prev``.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path

from shiftcodes.adapters.errors import DeployError

OVERRIDE_NAME = "override.yml"
PREVIOUS_SUFFIX = ".prev"
DIRECTORY_MODE = 0o750


def fsync_directory(path: Path) -> None:
    """Flush a directory entry (best effort where the platform allows opening directories)."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_atomically(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` so readers see the old or the new file, never a mix."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=DIRECTORY_MODE)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        Path(temporary).chmod(0o644)  # compose reads it; it holds image names only
        Path(temporary).replace(path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    fsync_directory(path.parent)


class OverrideStore:
    """Reads and writes the managed override of environments."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def path(self, compose_project: str) -> Path:
        """The override file of the environment that owns ``compose_project``."""
        return self._root / compose_project / OVERRIDE_NAME

    def current(self, compose_project: str) -> dict[str, str]:
        """Service to image of the override now on disk (empty when there is none)."""
        return self._read(self.path(compose_project))

    def previous(self, compose_project: str) -> dict[str, str]:
        """Service to image of the version before the last write."""
        path = self.path(compose_project)
        return self._read(path.with_name(path.name + PREVIOUS_SUFFIX))

    def write(self, compose_project: str, images: Mapping[str, str]) -> Path:
        """Store ``images`` as the new override and keep the replaced version."""
        path = self.path(compose_project)
        path.parent.mkdir(parents=True, exist_ok=True, mode=DIRECTORY_MODE)
        if path.exists():
            shutil.copyfile(path, path.with_name(path.name + PREVIOUS_SUFFIX))
        document = {"services": {name: {"image": image} for name, image in sorted(images.items())}}
        write_atomically(path, json.dumps(document, indent=2) + "\n")
        return path

    def file_for_compose(self, compose_project: str) -> Path | None:
        """The path to append to the compose files, or ``None`` when nothing was deployed yet."""
        path = self.path(compose_project)
        return path if path.exists() else None

    @staticmethod
    def _read(path: Path) -> dict[str, str]:
        if not path.exists():
            return {}
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            services = document["services"]
            return {str(name): str(entry["image"]) for name, entry in services.items()}
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            raise DeployError(f"the managed override {path} is unreadable: {error}") from error
