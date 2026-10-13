"""Run schemas: run, stages and steps; log chunks; cancel result."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from shiftcodes.domain.enums import PipelineSource, RunStatus, RunTrigger
from shiftcodes.execution.logs import LogChunk
from shiftcodes.repositories.models import Run, StepRun
from shiftcodes.services.project_queries import RunPage as RunPageData
from shiftcodes.services.project_queries import RunSummaryData
from shiftcodes.services.run_lifecycle import CancelOutcome
from shiftcodes.services.run_queries import RunDetail, StageDetail


class StepResponse(BaseModel):
    """One executed step."""

    id: int
    position: int
    step_type: str
    label: str
    status: RunStatus
    exit_code: int | None
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def from_step(cls, step: StepRun) -> StepResponse:
        """Build from a step record."""
        return cls(
            id=step.id,
            position=step.position,
            step_type=step.step_type,
            label=step.label,
            status=step.status,
            exit_code=step.exit_code,
            started_at=step.started_at,
            finished_at=step.finished_at,
        )


class StageResponse(BaseModel):
    """One stage with its steps."""

    id: int
    position: int
    name: str
    status: RunStatus
    started_at: datetime | None
    finished_at: datetime | None
    steps: list[StepResponse]

    @classmethod
    def from_detail(cls, detail: StageDetail) -> StageResponse:
        """Build from a stage and its steps."""
        stage = detail.stage
        return cls(
            id=stage.id,
            position=stage.position,
            name=stage.name,
            status=stage.status,
            started_at=stage.started_at,
            finished_at=stage.finished_at,
            steps=[StepResponse.from_step(step) for step in detail.steps],
        )


class RunSummary(BaseModel):
    """A run without its stages (lists and trigger results)."""

    id: int
    number: int
    project: str
    environment: str
    trigger: RunTrigger
    branch: str
    commit_sha: str
    pipeline_source: PipelineSource
    status: RunStatus
    message: str | None
    cancel_requested_at: datetime | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def from_run(cls, run: Run, project: str, environment: str) -> RunSummary:
        """Build from a run row and the names of its project and environment."""
        return cls(
            id=run.id,
            number=run.number,
            project=project,
            environment=environment,
            trigger=run.trigger,
            branch=run.branch,
            commit_sha=run.commit_sha,
            pipeline_source=run.pipeline_source,
            status=run.status,
            message=run.message,
            cancel_requested_at=run.cancel_requested_at,
            created_at=run.created_at,
            started_at=run.started_at,
            finished_at=run.finished_at,
        )

    @classmethod
    def from_data(cls, data: RunSummaryData) -> RunSummary:
        """Build from the run list read model."""
        return cls.from_run(data.run, data.project, data.environment)


class RunResponse(RunSummary):
    """A run with its stages and steps."""

    stages: list[StageResponse]

    @classmethod
    def from_detail(cls, detail: RunDetail) -> RunResponse:
        """Build from the run read model."""
        summary = RunSummary.from_run(detail.run, detail.project, detail.environment)
        stages = [StageResponse.from_detail(stage) for stage in detail.stages]
        return cls(**summary.model_dump(), stages=stages)


class RunPage(BaseModel):
    """One page of runs, newest first."""

    items: list[RunSummary]
    total: int
    limit: int
    offset: int

    @classmethod
    def from_page(cls, page: RunPageData) -> RunPage:
        """Build from the page read model."""
        return cls(
            items=[RunSummary.from_data(item) for item in page.items],
            total=page.total,
            limit=page.limit,
            offset=page.offset,
        )


class LogChunkResponse(BaseModel):
    """A bounded slice of a step log; continue with ``offset=next_offset``."""

    offset: int
    next_offset: int
    size: int
    text: str
    complete: bool

    @classmethod
    def from_chunk(cls, chunk: LogChunk, *, step_finished: bool) -> LogChunkResponse:
        """``complete`` is true when the step ended and nothing is left to read."""
        return cls(
            offset=chunk.offset,
            next_offset=chunk.next_offset,
            size=chunk.size,
            text=chunk.text,
            complete=step_finished and chunk.at_end,
        )


class CancelResponse(BaseModel):
    """Result of a cancel request."""

    outcome: CancelOutcome
    run: RunResponse
