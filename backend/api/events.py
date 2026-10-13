"""Server-Sent Events of a run: status changes and new log lines."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

from backend.api import authz
from backend.api.container import AppContainer
from backend.api.principal import Principal
from backend.api.schemas.runs import RunResponse
from backend.domain.access import Action, Resource
from backend.domain.enums import EnvironmentName
from backend.execution.logs import LogStore
from backend.services.run_queries import RunDetail, RunQueryService

MAX_STREAM_SECONDS = 3600.0


def sse(event: str, data: str) -> str:
    """Format one event (``data`` must be a single line)."""
    return f"event: {event}\ndata: {data}\n\n"


class RunEventStream:
    """Yields SSE text until the run finishes, the session ends or the lifetime is reached.

    It reads through the background principal path: opening a stream and keeping it open never
    extends a session's idle timer (the session is re-checked, without touching it, at every
    heartbeat).
    """

    def __init__(self, container: AppContainer, principal: Principal, run_id: int) -> None:
        self._container = container
        self._principal = principal
        self._run_id = run_id
        detail = container.run_queries().locate(run_id)
        self._project, self._environment = detail[0], detail[1].value
        self._queries: RunQueryService = container.run_queries()
        self._logs: LogStore = container.log_store()
        self._offsets: dict[int, int] = {}
        self._last_status = ""

    def __iter__(self) -> Iterator[str]:
        settings = self._container.settings
        started = time.monotonic()
        last_sent = started
        while time.monotonic() - started < MAX_STREAM_SECONDS:
            detail = self._queries.detail(self._run_id)
            events = list(self._events(detail))
            if events:
                last_sent = time.monotonic()
                yield from events
            if detail.run.status.is_finished:
                yield sse("end", json.dumps({"status": detail.run.status.value}))
                return
            if time.monotonic() - last_sent >= settings.sse_heartbeat_interval:
                if not self._session_alive():
                    return
                last_sent = time.monotonic()
                yield ": heartbeat\n\n"
            time.sleep(settings.sse_poll_interval)

    def _events(self, detail: RunDetail) -> Iterator[str]:
        status = RunResponse.from_detail(detail).model_dump_json()
        if status != self._last_status:
            self._last_status = status
            yield sse("status", status)
        for stage in detail.stages:
            for step in stage.steps:
                yield from self._log_events(step.id)

    def _log_events(self, step_id: int) -> Iterator[str]:
        while True:
            offset = self._offsets.get(step_id, 0)
            chunk = self._logs.read_chunk(self._run_id, step_id, offset)
            if not chunk.text:
                return
            self._offsets[step_id] = chunk.next_offset
            payload = {"step_id": step_id, "offset": chunk.offset, "text": chunk.text}
            yield sse("log", json.dumps(payload))
            if chunk.at_end:
                return

    def _session_alive(self) -> bool:
        """Still authenticated and still allowed to read the run (grants can be revoked)."""
        resource = Resource.of_environment(self._project, EnvironmentName(self._environment))
        return authz.still_allowed(self._container, self._principal, Action.RUN_READ, resource)
