"""Run state transitions: claim, finish, cancel and crash recovery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from backend.domain.actor import Actor
from backend.domain.audit import AuditEntry, AuditTarget
from backend.domain.clock import Clock, utcnow
from backend.domain.enums import RunStatus
from backend.domain.errors import ConflictError, NotFoundError
from backend.repositories.database import UnitOfWork
from backend.repositories.models import Run
from backend.services.audit import add_audit_event

MAX_MESSAGE = 500
CANCELLED_WHILE_QUEUED = "cancelled before it started"


class CancelOutcome(StrEnum):
    """What ``request_cancel`` did."""

    CANCELLED = "cancelled"  # a queued run was cancelled immediately
    REQUESTED = "requested"  # a running run was asked to stop; the worker finishes the job


@dataclass(frozen=True, slots=True)
class PreviousRun:
    """The latest finished run of the same environment before a given run."""

    id: int
    status: RunStatus


def clip_message(message: str) -> str:
    """Shorten ``message`` to what the column holds."""
    flat = " ".join(message.split())
    return flat if len(flat) <= MAX_MESSAGE else flat[: MAX_MESSAGE - 3] + "..."


class RunLifecycleService:
    """Moves runs through their states; every transition is a conditional update."""

    def __init__(self, uow: UnitOfWork, clock: Clock = utcnow) -> None:
        self._uow = uow
        self._clock = clock

    def claim_next(self) -> Run | None:
        """Mark the oldest queued run as running and return it."""
        with self._uow:
            return self._uow.runs.claim_oldest_queued(self._clock())

    def count_running(self) -> int:
        """Return the number of runs currently running."""
        with self._uow:
            return self._uow.runs.count_by_status(RunStatus.RUNNING)

    def finish(self, run_id: int, status: RunStatus, message: str | None = None) -> bool:
        """Record the final status of a running run; ``False`` when it was not running."""
        text = clip_message(message) if message else None
        with self._uow:
            return self._uow.runs.transition(
                run_id,
                {RunStatus.RUNNING},
                status=status,
                finished_at=self._clock(),
                message=text,
            )

    def request_cancel(self, run_id: int, actor: Actor) -> CancelOutcome:
        """Cancel a queued run now, or flag a running one for the worker; audited."""
        with self._uow:
            run = self._uow.runs.get(run_id)
            if run is None:
                raise NotFoundError(f"run not found: {run_id}")
            outcome = self._cancel(run)
            details: dict[str, object] = {"number": run.number, "outcome": outcome.value}
            entry = AuditEntry(actor, "run.cancel", AuditTarget("run", str(run_id)), details)
            add_audit_event(self._uow, entry, self._clock)
            return outcome

    def _cancel(self, run: Run) -> CancelOutcome:
        now = self._clock()
        queued = {RunStatus.QUEUED}
        if self._uow.runs.transition(
            run.id,
            queued,
            status=RunStatus.CANCELLED,
            finished_at=now,
            message=CANCELLED_WHILE_QUEUED,
        ):
            return CancelOutcome.CANCELLED
        running = {RunStatus.RUNNING}
        if self._uow.runs.transition(run.id, running, cancel_requested_at=now):
            return CancelOutcome.REQUESTED
        raise ConflictError(f"run {run.id} already finished ({self._status(run.id)})")

    def _status(self, run_id: int) -> str:
        run = self._uow.runs.get(run_id)
        return run.status.value if run else "unknown"

    def is_cancel_requested(self, run_id: int) -> bool:
        """Whether a cancel was requested for the run."""
        with self._uow:
            run = self._uow.runs.get(run_id)
            return run is not None and run.cancel_requested_at is not None

    def previous_run(self, run_id: int) -> PreviousRun | None:
        """The latest finished run of the same environment before ``run_id``, if any."""
        with self._uow:
            run = self._uow.runs.get(run_id)
            if run is None:
                return None
            previous = self._uow.runs.previous_finished(run.environment_id, run.number)
            return PreviousRun(previous.id, previous.status) if previous else None

    def previous_stage_status(self, previous_run_id: int, stage_name: str) -> RunStatus | None:
        """Status of the stage called ``stage_name`` in an earlier run."""
        with self._uow:
            stage = self._uow.runs.find_stage(previous_run_id, stage_name)
            return stage.status if stage else None

    def running_run_ids(self) -> list[int]:
        """Ids of the runs currently marked running."""
        with self._uow:
            return [run.id for run in self._uow.runs.list_by_status(RunStatus.RUNNING)]

    def interrupt_running(self, message: str) -> list[int]:
        """Mark every running run (and its unfinished stages and steps) as interrupted.

        Used at worker start: nothing can still be running. Returns the affected run ids;
        a second call finds nothing and returns an empty list.
        """
        now = self._clock()
        with self._uow:
            runs = self._uow.runs.list_by_status(RunStatus.RUNNING)
            ids = [run.id for run in runs]
            for run in runs:
                self._interrupt(run, now, clip_message(message))
            return ids

    def _interrupt(self, run: Run, now: datetime, message: str) -> None:
        self._uow.runs.transition(
            run.id,
            {RunStatus.RUNNING},
            status=RunStatus.INTERRUPTED,
            finished_at=now,
            message=message,
        )
        for stage in self._uow.runs.list_stages(run.id):
            for step in self._uow.runs.list_steps(stage.id):
                if step.status is RunStatus.RUNNING:
                    step.status = RunStatus.INTERRUPTED
                    step.finished_at = now
            if stage.status is RunStatus.RUNNING:
                stage.status = RunStatus.INTERRUPTED
                stage.finished_at = now
            elif stage.status is RunStatus.QUEUED:
                stage.status = RunStatus.SKIPPED
