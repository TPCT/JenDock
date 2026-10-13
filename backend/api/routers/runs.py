"""Run endpoints: detail, step logs, live events and cancellation."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from backend.api import authz
from backend.api.deps import Container, Target, require
from backend.api.events import RunEventStream
from backend.api.paging import Paging
from backend.api.principal import Principal
from backend.api.run_filters import GlobalRunFilters
from backend.api.schemas.runs import (
    CancelResponse,
    LogChunkResponse,
    RunPage,
    RunResponse,
    RunSummary,
)
from backend.domain.access import Action

router = APIRouter(prefix="/api/runs", tags=["runs"])
Lister = Annotated[Principal, Depends(require(Action.RUN_LIST, Target.NONE))]
Reader = Annotated[Principal, Depends(require(Action.RUN_READ, Target.RUN))]
BackgroundReader = Annotated[
    Principal, Depends(require(Action.RUN_READ, Target.RUN, background=True))
]
Canceller = Annotated[Principal, Depends(require(Action.RUN_CANCEL, Target.RUN))]
Retrier = Annotated[Principal, Depends(require(Action.RUN_RETRY, Target.RUN))]


@router.get("")
def list_runs(
    principal: Lister, container: Container, paging: Paging, filters: GlobalRunFilters
) -> RunPage:
    """Runs of every project and environment the caller may read (newest first), searchable
    and filterable.

    ``q`` matches project, branch, commit, message and run number; ``sort`` is ``created``,
    ``finished`` or ``number``. Access is applied before searching, paging and counting.
    """
    only = authz.run_predicate(principal)
    page = container.project_queries().all_runs(paging.query, filters.filter(), only)
    return RunPage.from_page(page)


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
    return container.step_log(run_id, step_id, offset)


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
def cancel_run(run_id: int, principal: Canceller, container: Container) -> CancelResponse:
    """Cancel a queued run now, or ask the worker to stop a running one."""
    outcome = container.run_lifecycle().request_cancel(run_id, principal.actor)
    return CancelResponse(
        outcome=outcome, run=RunResponse.from_detail(container.run_queries().detail(run_id))
    )


@router.post("/{run_id}/retry", status_code=201)
def retry_run(run_id: int, principal: Retrier, container: Container) -> RunSummary:
    """Queue a new run of the same commit and branch (trigger ``manual``) of a finished run."""
    run = container.run_triggers().retry(run_id, principal.run_origin())
    detail = container.run_queries().detail(run.id)
    return RunSummary.from_run(run, detail.project, detail.environment)
