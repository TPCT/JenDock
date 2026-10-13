"""Per-service managed overrides written as a Compose override file (Task 8).

This module owns exactly one file per environment: ``container-overrides.yml`` in
``<root>/<project>/<environment>/``. It holds what a user changed in the platform on top of the
repository's compose files: environment variables and CPU/memory limits per service.

Coexistence with other managed override files (for example the image override of the deploy
engine): every writer keeps its own file name in the same directory and every compose
invocation appends all of them after the repository's files, in a fixed order::

    -f <repo files...> -f container-overrides.yml -f <deploy image override>

The deploy engine's image override is always the last file so deployed images stay pinned.
``ComposeOverrideStore.compose_file_args`` returns this file's ``-f`` arguments.

The file is JSON, which is valid YAML and which Compose reads natively; that keeps escaping
exact. Values can be secrets: the directory is 0700 and the file 0600. A literal ``$`` in a value
is written as ``$$`` because Compose interpolates the file.
"""

from __future__ import annotations

import json
import os
import re
import secrets as secrets_module
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from backend.adapters.errors import AdapterError
from backend.domain.containers import EnvironmentKey

FILE_NAME = "container-overrides.yml"
DIR_MODE = 0o700
FILE_MODE = 0o600
FORMAT_VERSION = 1
_MARKER_KEY = "x-jendock-container-overrides"
_COMPONENT = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")


class OverrideFileError(AdapterError):
    """The override file is unreadable or cannot be written."""


@dataclass(frozen=True, slots=True)
class ServiceOverride:
    """What is overridden for one service. Empty means: nothing."""

    env: Mapping[str, str] = field(default_factory=dict)
    cpus: float | None = None
    memory_bytes: int | None = None

    @property
    def is_empty(self) -> bool:
        """Whether nothing is overridden."""
        return not self.env and self.cpus is None and self.memory_bytes is None


@dataclass(frozen=True, slots=True)
class OverrideSet:
    """All service overrides of one environment (immutable; ``with_*`` returns a copy)."""

    services: Mapping[str, ServiceOverride] = field(default_factory=dict)

    def for_service(self, service: str) -> ServiceOverride:
        """The override of ``service`` (empty when there is none)."""
        return self.services.get(service, ServiceOverride())

    def with_service(self, service: str, override: ServiceOverride) -> OverrideSet:
        """A copy with ``service`` replaced; an empty override removes the entry."""
        services = {name: item for name, item in self.services.items() if name != service}
        if not override.is_empty:
            services[service] = override
        return OverrideSet(dict(sorted(services.items())))

    @property
    def is_empty(self) -> bool:
        """Whether no service is overridden."""
        return not self.services

    def to_document(self) -> dict[str, object]:
        """The compose document (``$`` escaped for Compose's interpolation)."""
        services: dict[str, object] = {}
        for name, override in self.services.items():
            entry: dict[str, object] = {}
            if override.env:
                entry["environment"] = {
                    key: value.replace("$", "$$") for key, value in sorted(override.env.items())
                }
            if override.cpus is not None:
                entry["cpus"] = override.cpus
            if override.memory_bytes is not None:
                entry["mem_limit"] = override.memory_bytes
            services[name] = entry
        return {_MARKER_KEY: {"version": FORMAT_VERSION}, "services": services}

    @classmethod
    def from_document(cls, document: object) -> OverrideSet:
        """Parse a document written by ``to_document``; anything else is an error."""
        if not isinstance(document, dict) or _MARKER_KEY not in document:
            raise OverrideFileError("override file was not written by the platform")
        services = document.get("services", {})
        if not isinstance(services, dict):
            raise OverrideFileError("override file is malformed")
        parsed: dict[str, ServiceOverride] = {}
        for name, entry in services.items():
            if not isinstance(entry, dict):
                raise OverrideFileError("override file is malformed")
            parsed[str(name)] = _parse_service(entry)
        return cls(dict(sorted(parsed.items())))


def _parse_service(entry: Mapping[str, object]) -> ServiceOverride:
    env = entry.get("environment", {})
    cpus = entry.get("cpus")
    memory = entry.get("mem_limit")
    if not isinstance(env, dict) or not all(isinstance(v, str) for v in env.values()):
        raise OverrideFileError("override file is malformed")
    if cpus is not None and (isinstance(cpus, bool) or not isinstance(cpus, int | float)):
        raise OverrideFileError("override file is malformed")
    if memory is not None and (isinstance(memory, bool) or not isinstance(memory, int)):
        raise OverrideFileError("override file is malformed")
    return ServiceOverride(
        env={str(k): str(v).replace("$$", "$") for k, v in env.items()},
        cpus=float(cpus) if cpus is not None else None,
        memory_bytes=memory,
    )


class ComposeOverrideStore:
    """Reads and writes the override file of each environment."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def directory(self, key: EnvironmentKey) -> Path:
        """The directory that holds every managed override file of the environment."""
        for part in (key.project, key.environment.value):
            if _COMPONENT.fullmatch(part) is None:
                raise OverrideFileError(f"invalid path component: {part!r}")
        return self._root / key.project / key.environment.value

    def path(self, key: EnvironmentKey) -> Path:
        """The override file (it may not exist)."""
        return self.directory(key) / FILE_NAME

    def load(self, key: EnvironmentKey) -> OverrideSet:
        """The current overrides; an absent file means none."""
        snapshot = self.snapshot(key)
        if snapshot is None:
            return OverrideSet()
        try:
            return OverrideSet.from_document(json.loads(snapshot))
        except ValueError as error:
            raise OverrideFileError("override file is not valid") from error

    def snapshot(self, key: EnvironmentKey) -> bytes | None:
        """The raw bytes of the file, or ``None`` when it does not exist."""
        try:
            return self.path(key).read_bytes()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise OverrideFileError("cannot read the override file") from error

    def save(self, key: EnvironmentKey, overrides: OverrideSet) -> None:
        """Replace the file atomically; with nothing left to override the file is removed."""
        if overrides.is_empty:
            self.restore(key, None)
            return
        text = json.dumps(overrides.to_document(), indent=2, sort_keys=True) + "\n"
        self.restore(key, text.encode())

    def restore(self, key: EnvironmentKey, snapshot: bytes | None) -> None:
        """Put back exactly ``snapshot`` (``None`` deletes the file)."""
        path = self.path(key)
        try:
            if snapshot is None:
                path.unlink(missing_ok=True)
                return
            path.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
            _write_atomically(path, snapshot)
        except OSError as error:
            raise OverrideFileError("cannot write the override file") from error

    def compose_file_args(self, key: EnvironmentKey) -> list[str]:
        """``["-f", path]`` when an override file exists, otherwise nothing."""
        path = self.path(key)
        return ["-f", str(path)] if path.is_file() else []


def _write_atomically(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{secrets_module.token_hex(4)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
    try:
        os.fchmod(fd, FILE_MODE)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
