"""Read models of runs for the API: run, stages and steps."""

from __future__ import annotations

from dataclasses import dataclass

from backend.domain.errors import NotFoundError
from backend.repositories.database import UnitOfWork
from backend.repositories.models import Run, StageRun, StepRun


@dataclass(frozen=True, slots=True)
class StageDetail:
    """A stage with its steps."""

    stage: StageRun
    steps: tuple[StepRun, ...]


@dataclass(frozen=True, slots=True)
class RunDetail:
    """A run with the names of its project and environment, and its stages."""

    run: Run
    project: str
    environment: str
    stages: tuple[StageDetail, ...]


class RunQueryService:
    """Loads runs for display."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    def detail(self, run_id: int) -> RunDetail:
        """Return the run with stages and steps; raises ``NotFoundError``."""
        with self._uow:
            run = self._uow.runs.get(run_id)
            if run is None:
                raise NotFoundError(f"run not found: {run_id}")
            environment = self._uow.environments.get(run.environment_id)
            project = self._uow.projects.get(environment.project_id) if environment else None
            if environment is None or project is None:
                raise NotFoundError(f"environment of run {run_id} not found")
            stages = tuple(
                StageDetail(stage, tuple(self._uow.runs.list_steps(stage.id)))
                for stage in self._uow.runs.list_stages(run_id)
            )
            return RunDetail(run, project.slug, environment.name.value, stages)

    def require_step(self, run_id: int, step_id: int) -> StepRun:
        """Return the step if it belongs to the run; otherwise ``NotFoundError``."""
        with self._uow:
            step = self._uow.runs.get_step(step_id)
            stage = self._uow.runs.get_stage(step.stage_run_id) if step else None
            if step is None or stage is None or stage.run_id != run_id:
                raise NotFoundError(f"step not found: {step_id}")
            return step
