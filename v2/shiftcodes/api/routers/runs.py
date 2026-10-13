"""Run endpoints: detail, step logs, live events and cancellation."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from shiftcodes.api.deps import Container, require_scope
from shiftcodes.api.events import RunEventStream
from shiftcodes.api.principal import Principal
from shiftcodes.api.schemas.runs import (
    CancelResponse,
    LogChunkResponse,
    RunPage,
    RunResponse,
    RunSummary,
)
from shiftcodes.domain.enums import TokenScope
from shiftcodes.execution.logs import CHUNK_BYTES

router = APIRouter(prefix="/api/runs", tags=["runs"])
Reader = Annotated[Principal, Depends(require_scope(TokenScope.READ))]
BackgroundReader = Annotated[Principal, Depends(require_scope(TokenScope.READ, background=True))]
Runner = Annotated[Principal, Depends(require_scope(TokenScope.RUN))]
MAX_PAGE = 100


@router.get("")
def list_runs(
    _principal: Reader,
    container: Container,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RunPage:
    """Runs of every project and environment, newest first."""
    return RunPage.from_page(container.project_queries().all_runs(limit=limit, offset=offset))


@router.get("/{run_id}")
def get_run(run_id: int, _principal: Reader, container: Container) -> RunResponse:
    """The run with its stages and steps."""
    return RunResponse.from_detail(container.run_queries().detail(run_id))


@router.get("/{run_id}/steps/{step_id}/log")
def get_step_log(
    run_id: int,
    step_id: int,
    _principal: Reader,
    container: Container,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> LogChunkResponse:
    """A bounded chunk of a step's log starting at byte ``offset`` (already masked)."""
    step = container.run_queries().require_step(run_id, step_id)
    chunk = container.log_store().read_chunk(run_id, step_id, offset, CHUNK_BYTES)
    return LogChunkResponse.from_chunk(chunk, step_finished=step.status.is_finished)


@router.get("/{run_id}/events")
def run_events(run_id: int, principal: BackgroundReader, container: Container) -> StreamingResponse:
    """Server-Sent Events: ``status`` snapshots, ``log`` lines, heartbeats and a final ``end``."""
    container.run_queries().detail(run_id)  # 404 before the stream starts
    stream = RunEventStream(container, principal, run_id)
    return StreamingResponse(
        iter(stream),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.post("/{run_id}/cancel")
def cancel_run(run_id: int, principal: Runner, container: Container) -> CancelResponse:
    """Cancel a queued run now, or ask the worker to stop a running one."""
    outcome = container.run_lifecycle().request_cancel(run_id, principal.actor)
    return CancelResponse(
        outcome=outcome, run=RunResponse.from_detail(container.run_queries().detail(run_id))
    )


@router.post("/{run_id}/retry", status_code=201)
def retry_run(run_id: int, principal: Runner, container: Container) -> RunSummary:
    """Queue a new run of the same commit and branch (trigger ``manual``) of a finished run."""
    run = container.run_triggers().retry(run_id, principal.run_origin())
    detail = container.run_queries().detail(run.id)
    return RunSummary.from_run(run, detail.project, detail.environment)
