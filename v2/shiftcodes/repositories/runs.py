"""Run, stage and step queries."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.orm import Session

from shiftcodes.domain.enums import RunStatus
from shiftcodes.repositories.models import Run, StageRun, StepRun


class RunRepository:
    """Queries for ``Run``, ``StageRun`` and ``StepRun``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, run: Run) -> Run:
        """Persist ``run`` and assign its id."""
        self._session.add(run)
        self._session.flush()
        return run

    def add_stage(self, stage: StageRun) -> StageRun:
        """Persist a stage record."""
        self._session.add(stage)
        self._session.flush()
        return stage

    def add_step(self, step: StepRun) -> StepRun:
        """Persist a step record."""
        self._session.add(step)
        self._session.flush()
        return step

    def get(self, run_id: int) -> Run | None:
        """Return the run with ``run_id`` or ``None``."""
        return self._session.get(Run, run_id)

    def last_number(self, environment_id: int) -> int:
        """Return the highest run number of an environment, or 0 when none exists."""
        query = select(func.max(Run.number)).where(Run.environment_id == environment_id)
        return self._session.scalar(query) or 0

    def list_for_environment(
        self, environment_id: int, *, limit: int = 50, offset: int = 0
    ) -> Sequence[Run]:
        """Return runs of an environment, newest first."""
        query = (
            select(Run)
            .where(Run.environment_id == environment_id)
            .order_by(Run.number.desc())
            .limit(limit)
            .offset(offset)
        )
        return self._session.scalars(query).all()

    def list_by_status(self, status: RunStatus) -> Sequence[Run]:
        """Return runs in ``status``, oldest first."""
        query = select(Run).where(Run.status == status).order_by(Run.created_at, Run.id)
        return self._session.scalars(query).all()

    def claim_oldest_queued(self, now: datetime) -> Run | None:
        """Atomically move the oldest queued run to running; ``None`` when nothing is queued.

        The conditional update makes a concurrent cancel or a second claimer lose cleanly.
        """
        query = (
            select(Run.id)
            .where(Run.status == RunStatus.QUEUED)
            .order_by(Run.created_at, Run.id)
            .limit(1)
        )
        run_id = self._session.scalar(query)
        if run_id is None:
            return None
        if not self.transition(
            run_id, {RunStatus.QUEUED}, status=RunStatus.RUNNING, started_at=now
        ):
            return None
        return self.get(run_id)

    def transition(self, run_id: int, allowed: set[RunStatus], **values: object) -> bool:
        """Set ``values`` when the run is currently in one of ``allowed``; report success."""
        statement = (
            update(Run)
            .where(Run.id == run_id, Run.status.in_(allowed))
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        result = self._session.execute(statement)
        changed = isinstance(result, CursorResult) and result.rowcount > 0
        self._session.expire_all()
        return changed

    def count_by_status(self, status: RunStatus) -> int:
        """Return how many runs are in ``status``."""
        query = select(func.count()).select_from(Run).where(Run.status == status)
        return self._session.scalar(query) or 0

    def previous_finished(self, environment_id: int, number: int) -> Run | None:
        """Return the latest finished run of the environment before run ``number``."""
        finished = [s for s in RunStatus if s.is_finished]
        query = (
            select(Run)
            .where(
                Run.environment_id == environment_id,
                Run.number < number,
                Run.status.in_(finished),
            )
            .order_by(Run.number.desc())
            .limit(1)
        )
        return self._session.scalar(query)

    def get_stage(self, stage_run_id: int) -> StageRun | None:
        """Return one stage record."""
        return self._session.get(StageRun, stage_run_id)

    def get_step(self, step_run_id: int) -> StepRun | None:
        """Return one step record."""
        return self._session.get(StepRun, step_run_id)

    def find_stage(self, run_id: int, name: str) -> StageRun | None:
        """Return the stage of a run called ``name`` (display names are unique per run)."""
        query = select(StageRun).where(StageRun.run_id == run_id, StageRun.name == name)
        return self._session.scalar(query)

    def next_step_position(self, stage_run_id: int) -> int:
        """Return the position for the next step of a stage (1-based)."""
        query = select(func.max(StepRun.position)).where(StepRun.stage_run_id == stage_run_id)
        return (self._session.scalar(query) or 0) + 1

    def list_stages(self, run_id: int) -> Sequence[StageRun]:
        """Return the stages of a run in position order."""
        query = select(StageRun).where(StageRun.run_id == run_id).order_by(StageRun.position)
        return self._session.scalars(query).all()

    def list_steps(self, stage_run_id: int) -> Sequence[StepRun]:
        """Return the steps of a stage in position order."""
        query = (
            select(StepRun).where(StepRun.stage_run_id == stage_run_id).order_by(StepRun.position)
        )
        return self._session.scalars(query).all()
