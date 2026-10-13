"""Server-Sent Events that follow a container's log by polling the worker for new lines."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

from backend.api.container import AppContainer
from backend.api.events import sse
from backend.api.principal import Principal
from backend.containers.logs import LogCursor
from backend.containers.wire import LogPageView
from backend.domain.enums import EnvironmentName
from backend.domain.errors import DomainError

MAX_STREAM_SECONDS = 3600.0
MIN_POLL_SECONDS = 1.0


class ContainerLogStream:
    """Yields SSE text: ``log`` events (masked lines), heartbeats, ``error`` and ``end``.

    Bounded in time (one hour), rate (the worker returns at most 500 lines per poll and a poll
    happens at most once per second; surplus lines are reported as ``dropped``) and size (long
    lines are cut). It authenticates through the background principal path: following a log never
    extends a session's idle timer, and the session is re-checked at every heartbeat.
    """

    def __init__(  # noqa: PLR0913, PLR0917 - the stream's fixed context
        self,
        container: AppContainer,
        principal: Principal,
        slug: str,
        env: EnvironmentName,
        container_id: str,
        tail: int,
    ) -> None:
        self._container = container
        self._principal = principal
        self._slug = slug
        self._env = env
        self._container_id = container_id
        self._tail = tail

    def __iter__(self) -> Iterator[str]:
        settings = self._container.settings
        poll = max(settings.sse_poll_interval, MIN_POLL_SECONDS)
        started = last_sent = last_check = time.monotonic()
        cursor: LogCursor | None = None
        while time.monotonic() - started < MAX_STREAM_SECONDS:
            try:
                page = self._read(cursor)
            except DomainError as error:
                yield sse("error", json.dumps({"message": str(error)}))
                return
            if page.cursor is not None:
                cursor = LogCursor(page.cursor.since, page.cursor.skip)
            if page.lines or page.dropped:
                last_sent = time.monotonic()
                yield sse("log", page.model_dump_json(include={"lines", "dropped"}))
            if time.monotonic() - last_check >= settings.sse_heartbeat_interval:
                if not self._session_alive():
                    return
                last_check = time.monotonic()
                if time.monotonic() - last_sent >= settings.sse_heartbeat_interval:
                    last_sent = last_check
                    yield ": heartbeat\n\n"
            time.sleep(poll)
        yield sse("end", json.dumps({"reason": "time limit"}))

    def _read(self, cursor: LogCursor | None) -> LogPageView:
        gateway = self._container.container_gateway()
        return gateway.logs(
            self._slug,
            self._env,
            self._container_id,
            self._principal.actor,
            tail=self._tail,
            cursor=cursor,
        )

    def _session_alive(self) -> bool:
        token = self._principal.session_token
        if token is None:
            return True
        return self._container.session_service().resolve(token, touch=False) is not None
