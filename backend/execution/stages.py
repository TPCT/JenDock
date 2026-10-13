"""Stage execution: ``when``, environment, options, bodies (steps, sequential, parallel), post."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from backend.domain.enums import RunStatus
from backend.execution.conditions import WhenEvaluator
from backend.execution.context import RunContext
from backend.execution.errors import RunCancelled, StepFailed
from backend.execution.executor import StepExecutor
from backend.execution.options import StageOptions
from backend.execution.plan import StagePlan
from backend.execution.post import PostRunner
from backend.execution.scope import FailureFlag, Scope
from backend.execution.scope_builder import apply_agent, apply_environment
from backend.execution.state import RunHistory, RunState
from backend.pipeline.ast import ParallelBody, SequentialBody, Stage, StepsBody

logger = logging.getLogger(__name__)
StagePath = tuple[str, ...]


class StageRunner:
    """Runs the stages of a pipeline for one run."""

    def __init__(
        self,
        run: RunContext,
        executor: StepExecutor,
        plan: StagePlan,
        state: RunState,
        history: RunHistory,
    ) -> None:
        self._run = run
        self._executor = executor
        self._plan = plan
        self._state = state
        self._history = history
        self._when = WhenEvaluator(run.job.branch)
        self._post = PostRunner(executor, state)

    def run_stages(
        self, stages: tuple[Stage, ...], scope: Scope, prefix: StagePath
    ) -> list[RunStatus]:
        """Run sibling stages in order."""
        return [self.run_stage(stage, scope, (*prefix, stage.name)) for stage in stages]

    def run_stage(self, stage: Stage, parent: Scope, path: StagePath) -> RunStatus:
        """Run one stage and return its final status (also recorded)."""
        recorder = self._run.recorder
        stage_id = self._plan.id_for(path)
        if parent.flag.is_set() or self._run.cancel.is_set():
            recorder.finish_stage(stage_id, RunStatus.SKIPPED)
            return RunStatus.SKIPPED
        if not self._when.matches(stage.when, parent):
            recorder.finish_stage(stage_id, RunStatus.SKIPPED)
            return RunStatus.SKIPPED
        recorder.start_stage(stage_id)
        status, scope = self._execute(stage, parent, stage_id, path)
        status = self._finish_post(stage, scope, stage_id, status, path)
        if status is RunStatus.FAILED:
            parent.flag.set()
        recorder.finish_stage(stage_id, status)
        return status

    def _execute(
        self, stage: Stage, parent: Scope, stage_id: int, path: StagePath
    ) -> tuple[RunStatus, Scope]:
        scope = parent
        try:
            scope = apply_agent(parent, stage.agent)
            scope = apply_environment(self._run, stage.environment, scope)
            result = [RunStatus.SUCCEEDED]
            options = StageOptions.parse(stage.options)
            mark = self._state.snapshot()

            def body(inner: Scope) -> None:
                fresh = replace(inner, flag=FailureFlag())
                result[0] = self._body(stage, fresh, stage_id, path)

            options.apply(scope, body, lambda _n, _e: self._state.restore(mark))
            return result[0], scope
        except RunCancelled:
            return RunStatus.CANCELLED, scope
        except StepFailed as error:
            self._state.fail(f"stage '{stage.name}' failed: {error}")
            return RunStatus.FAILED, scope

    def _body(self, stage: Stage, scope: Scope, stage_id: int, path: StagePath) -> RunStatus:
        body = stage.body
        if isinstance(body, StepsBody):
            self._executor.run_all(body.steps, scope, stage_id)
            return RunStatus.SUCCEEDED
        if isinstance(body, SequentialBody):
            return self._group(self.run_stages(body.stages, scope, path))
        if isinstance(body, ParallelBody):
            return self._group(self._parallel(body, scope, path))
        raise TypeError(f"unknown stage body: {body!r}")

    def _parallel(self, body: ParallelBody, scope: Scope, path: StagePath) -> list[RunStatus]:
        workers = self._run.config.parallel_workers
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="branch") as pool:
            futures = [
                pool.submit(
                    self.run_stage, branch, replace(scope, flag=FailureFlag()), (*path, branch.name)
                )
                for branch in body.stages
            ]
            results = [future.exception() or future.result() for future in futures]
        errors = [item for item in results if isinstance(item, BaseException)]
        if errors:
            raise errors[0]
        return [item for item in results if isinstance(item, RunStatus)]

    @staticmethod
    def _group(statuses: list[RunStatus]) -> RunStatus:
        """Fold the statuses of nested stages; failures and cancellation propagate upwards."""
        if RunStatus.FAILED in statuses:
            raise StepFailed("a nested stage failed")
        if RunStatus.CANCELLED in statuses:
            raise RunCancelled
        if statuses and all(status is RunStatus.SKIPPED for status in statuses):
            return RunStatus.SKIPPED
        return RunStatus.SUCCEEDED

    def _finish_post(
        self, stage: Stage, scope: Scope, stage_id: int, status: RunStatus, path: StagePath
    ) -> RunStatus:
        if stage.post is None:
            return status
        name = " / ".join(path)

        def changed() -> bool:
            previous = self._history.previous_stage_status(name)
            return previous is not None and previous is not status

        if status is RunStatus.SKIPPED:
            return status
        ok = self._post.run(stage.post, scope, stage_id, status, changed)
        return status if ok or status is not RunStatus.SUCCEEDED else RunStatus.FAILED
