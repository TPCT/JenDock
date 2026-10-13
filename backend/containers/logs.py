"""Container logs: a bounded, masked tail and a cursor for following."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC

from backend.adapters.container_admin import ContainerEngine
from backend.adapters.docker_inspect import parse_env
from backend.adapters.errors import ContainerRuntimeError
from backend.containers.base import Membership, TargetResolver
from backend.containers.masking import LogMasker
from backend.domain.clock import Clock, utcnow
from backend.domain.enums import EnvironmentName
from backend.domain.errors import ConflictError

MAX_TAIL = 1000
DEFAULT_TAIL = 200
MAX_LINES_PER_POLL = 500
MAX_LINE_CHARS = 8192
TRUNCATED = "...[line truncated]"
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T[0-9:.]+Z")


@dataclass(frozen=True, slots=True)
class LogLine:
    """One log line: Docker's timestamp (RFC 3339, nanoseconds) and the masked text."""

    timestamp: str
    text: str


@dataclass(frozen=True, slots=True)
class LogCursor:
    """Where a follower stopped: the last timestamp and how many lines of it were delivered."""

    since: str
    skip: int


@dataclass(frozen=True, slots=True)
class LogPage:
    """A batch of lines, the cursor to continue with and how many lines were left out."""

    lines: tuple[LogLine, ...]
    cursor: LogCursor | None
    dropped: int


def parse_line(raw: str) -> tuple[str, str]:
    """Split ``<timestamp> <text>``; a line without a timestamp keeps an empty one."""
    stamp, space, text = raw.partition(" ")
    if space and _TIMESTAMP.fullmatch(stamp):
        return stamp, text
    return "", raw


def next_cursor(entries: list[tuple[str, str]], previous: LogCursor | None) -> LogCursor | None:
    """The cursor after ``entries`` (timestamp, text) were seen."""
    stamped = [stamp for stamp, _text in entries if stamp]
    if not stamped:
        return previous
    last = stamped[-1]
    return LogCursor(last, sum(1 for stamp in stamped if stamp == last))


def select_new(entries: list[tuple[str, str]], cursor: LogCursor | None) -> list[tuple[str, str]]:
    """Drop what the follower already received (lines at or before the cursor)."""
    if cursor is None:
        return entries
    skipped = 0
    fresh = []
    for stamp, text in entries:
        if stamp and stamp < cursor.since:
            continue
        if stamp == cursor.since and skipped < cursor.skip:
            skipped += 1
            continue
        fresh.append((stamp, text))
    return fresh


class LogService:
    """Reads container logs through the engine and masks them."""

    def __init__(
        self,
        resolver: TargetResolver,
        engine: ContainerEngine,
        membership: Membership,
        clock: Clock = utcnow,
    ) -> None:
        self._resolver = resolver
        self._engine = engine
        self._membership = membership
        self._clock = clock

    def read(
        self,
        slug: str,
        env: EnvironmentName,
        container_id: str,
        *,
        tail: int = DEFAULT_TAIL,
        cursor: LogCursor | None = None,
    ) -> LogPage:
        """The last ``tail`` lines, or the lines after ``cursor``; always bounded and masked."""
        tail = max(1, min(tail, MAX_TAIL))
        target = self._resolver.resolve(slug, env)
        record = self._membership.require(target, container_id)
        try:
            raw = self._engine.logs(
                container_id,
                tail=MAX_TAIL if cursor else tail,
                since=cursor.since if cursor else None,
            )
        except ContainerRuntimeError as error:
            raise ConflictError(str(error)) from error
        entries = [parse_line(line) for line in raw]
        fresh = select_new(entries, cursor)
        new_cursor = next_cursor(entries, cursor) or LogCursor(self._now(), 0)
        limit = MAX_LINES_PER_POLL if cursor else tail
        dropped = max(0, len(fresh) - limit)
        masker = LogMasker(parse_env(record))
        kept = fresh[max(0, len(fresh) - limit) :]
        lines = tuple(LogLine(stamp, _bound(masker.mask(text))) for stamp, text in kept)
        return LogPage(lines, new_cursor, dropped)

    def _now(self) -> str:
        return self._clock().astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _bound(text: str) -> str:
    return text if len(text) <= MAX_LINE_CHARS else text[:MAX_LINE_CHARS] + TRUNCATED
