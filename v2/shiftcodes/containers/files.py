"""Read-only file browser of a running container (no shell, no writes, size capped)."""

from __future__ import annotations

from dataclasses import dataclass

from shiftcodes.adapters.container_admin import ContainerEngine
from shiftcodes.adapters.docker_inspect import parse_container
from shiftcodes.adapters.errors import ContainerNotFoundError, ContainerRuntimeError
from shiftcodes.containers.base import ContainerAuditor, Membership, TargetResolver
from shiftcodes.domain.actor import Actor
from shiftcodes.domain.containers import is_protected_name, validate_container_path
from shiftcodes.domain.enums import EnvironmentName
from shiftcodes.domain.errors import ConflictError, NotFoundError, ValidationError

MAX_ENTRIES = 500
LIST_OUTPUT_BYTES = 128 * 1024
DEFAULT_READ_BYTES = 64 * 1024
MAX_READ_BYTES = 256 * 1024
BINARY_SNIFF_BYTES = 8192
MAX_NAME_LENGTH = 255
_UTF8_TAIL_BYTES = 3


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    """One name in a directory; ``kind`` is ``directory`` or ``file`` (links count as files)."""

    name: str
    kind: str


@dataclass(frozen=True, slots=True)
class DirectoryListing:
    """The entries of a directory, sorted, capped at ``MAX_ENTRIES``."""

    path: str
    entries: tuple[DirectoryEntry, ...]
    truncated: bool


@dataclass(frozen=True, slots=True)
class FileContent:
    """A file's size and, unless it is binary, its (possibly cut) text."""

    path: str
    size: int
    truncated: bool
    binary: bool
    text: str | None


class FileBrowser:
    """Lists directories (``ls`` in the container) and reads files (``docker cp``)."""

    def __init__(
        self,
        resolver: TargetResolver,
        engine: ContainerEngine,
        membership: Membership,
        auditor: ContainerAuditor,
    ) -> None:
        self._resolver = resolver
        self._engine = engine
        self._membership = membership
        self._auditor = auditor

    def list(
        self, slug: str, env: EnvironmentName, container_id: str, path: str
    ) -> DirectoryListing:
        """The entries of ``path``; the container must be running."""
        clean = validate_container_path(path)
        target = self._resolver.resolve(slug, env)
        record = self._membership.require(target, container_id)
        if not parse_container(record).running:
            raise ConflictError("start the container to browse its directories")
        try:
            raw = self._engine.list_directory(container_id, clean, LIST_OUTPUT_BYTES)
        except ContainerNotFoundError as error:
            raise NotFoundError("path not found") from error
        except ContainerRuntimeError as error:
            raise ConflictError(str(error)) from error
        return _listing(clean, raw)

    def read(  # noqa: PLR0913 - where, what and how much
        self,
        slug: str,
        env: EnvironmentName,
        container_id: str,
        path: str,
        actor: Actor,
        *,
        max_bytes: int = DEFAULT_READ_BYTES,
    ) -> FileContent:
        """Up to ``max_bytes`` of a regular file; the read is audited."""
        clean = validate_container_path(path)
        if clean == "/":
            raise ValidationError("not a file")
        limit = max(1, min(max_bytes, MAX_READ_BYTES))
        target = self._resolver.resolve(slug, env)
        self._membership.require(target, container_id)
        try:
            result = self._engine.read_file(container_id, clean, limit)
        except ContainerNotFoundError as error:
            raise NotFoundError("path not found") from error
        except ContainerRuntimeError as error:
            raise ConflictError(str(error)) from error
        if result.kind != "file":
            raise ValidationError(f"not a regular file ({result.kind})")
        self._auditor.record(
            actor, "container.file.read", target, container_id, {"path": clean, "size": result.size}
        )
        return _content(clean, result.size, result.data, result.truncated)


def _listing(path: str, lines: list[str]) -> DirectoryListing:
    entries: list[DirectoryEntry] = []
    for line in lines:
        name, kind = (line[:-1], "directory") if line.endswith("/") else (line, "file")
        invalid = not name or len(name) > MAX_NAME_LENGTH or any(ord(c) < 32 for c in name)
        if invalid or is_protected_name(name):
            continue
        entries.append(DirectoryEntry(name, kind))
    entries.sort(key=lambda entry: (entry.kind != "directory", entry.name))
    return DirectoryListing(path, tuple(entries[:MAX_ENTRIES]), len(entries) > MAX_ENTRIES)


def _content(path: str, size: int, data: bytes, truncated: bool) -> FileContent:
    if b"\0" in data[:BINARY_SNIFF_BYTES]:
        return FileContent(path, size, truncated, True, None)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        if truncated and error.start >= len(data) - _UTF8_TAIL_BYTES:
            return FileContent(path, size, truncated, False, data[: error.start].decode("utf-8"))
        return FileContent(path, size, truncated, True, None)
    return FileContent(path, size, truncated, False, text)
