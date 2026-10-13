"""Stage and step records written while a run executes.

Parallel branches report concurrently, so every call opens its own unit of work.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from backend.domain.clock import Clock, utcnow
from backend.domain.enums import RunStatus
from backend.repositories.database import UnitOfWork
from backend.repositories.models import StageRun, StepRun

UowFactory = Callable[[], UnitOfWork]
MAX_LABEL = 500
MAX_NAME = 200


class RunRecorder:
    """Persists stage and step progress of one worker process."""

    def __init__(self, uow_factory: UowFactory, clock: Clock = utcnow) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    def plan_stages(self, run_id: int, names: Sequence[str]) -> list[int]:
        """Create queued stage rows in display order and return their ids."""
        with self._uow_factory() as uow:
            return [
                uow.runs.add_stage(StageRun(run_id=run_id, position=index, name=name[:MAX_NAME])).id
                for index, name in enumerate(names, start=1)
            ]

    def start_stage(self, stage_id: int) -> None:
        """Mark a stage as running (restarting it clears an earlier finish time)."""
        with self._uow_factory() as uow:
            stage = _stage(uow, stage_id)
            stage.status = RunStatus.RUNNING
            stage.started_at = self._clock()
            stage.finished_at = None

    def finish_stage(self, stage_id: int, status: RunStatus) -> None:
        """Record the final status of a stage."""
        with self._uow_factory() as uow:
            stage = _stage(uow, stage_id)
            stage.status = status
            stage.finished_at = self._clock()
            if stage.started_at is None:
                stage.started_at = stage.finished_at

    def reset_stages(self, stage_ids: Sequence[int]) -> None:
        """Put stages back to queued (a retried pipeline starts over)."""
        with self._uow_factory() as uow:
            for stage_id in stage_ids:
                stage = _stage(uow, stage_id)
                stage.status = RunStatus.QUEUED
                stage.started_at = None
                stage.finished_at = None

    def skip_pending(self, run_id: int) -> None:
        """Mark stages that never started as skipped."""
        with self._uow_factory() as uow:
            for stage in uow.runs.list_stages(run_id):
                if stage.status is RunStatus.QUEUED:
                    stage.status = RunStatus.SKIPPED

    def start_step(self, stage_id: int, step_type: str, label: str) -> int:
        """Create a running step row and return its id."""
        with self._uow_factory() as uow:
            step = StepRun(
                stage_run_id=stage_id,
                position=uow.runs.next_step_position(stage_id),
                step_type=step_type,
                label=label[:MAX_LABEL],
                status=RunStatus.RUNNING,
                started_at=self._clock(),
            )
            return uow.runs.add_step(step).id

    def finish_step(self, step_id: int, status: RunStatus, exit_code: int | None) -> None:
        """Record the final status (and exit code, for scripts) of a step."""
        with self._uow_factory() as uow:
            step = uow.runs.get_step(step_id)
            if step is None:
                raise LookupError(f"step not found: {step_id}")
            step.status = status
            step.exit_code = exit_code
            step.finished_at = self._clock()


def _stage(uow: UnitOfWork, stage_id: int) -> StageRun:
    stage = uow.runs.get_stage(stage_id)
    if stage is None:
        raise LookupError(f"stage not found: {stage_id}")
    return stage
