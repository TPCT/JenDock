"""The pipeline runner: checkout, stages, post actions and the final outcome of a run."""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from shiftcodes.domain.enums import RunStatus
from shiftcodes.execution.cancel import CancelReason
from shiftcodes.execution.context import RunContext
from shiftcodes.execution.errors import RunCancelled, StepFailed
from shiftcodes.execution.executor import StepExecutor
from shiftcodes.execution.logs import LogStore
from shiftcodes.execution.options import StageOptions
from shiftcodes.execution.plan import StagePlan
from shiftcodes.execution.post import PostRunner
from shiftcodes.execution.scope import FailureFlag, Scope
from shiftcodes.execution.scope_builder import (
    apply_agent,
    apply_environment,
    builtin_env,
    default_params,
)
from shiftcodes.execution.stages import StageRunner
from shiftcodes.execution.state import RunHistory, RunState
from shiftcodes.execution.steps_checkout import CheckoutStep
from shiftcodes.pipeline.ast import Pipeline

logger = logging.getLogger(__name__)
CANCELLED_MESSAGE = "cancelled"
SHUTDOWN_MESSAGE = "interrupted: the worker was shut down while the run was in progress"


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """Final status of a run and a short explanation."""

    status: RunStatus
    message: str | None


class PipelineRunner:
    """Executes a parsed and validated ``Pipeline`` for one run."""

    def __init__(self, run: RunContext, logs: LogStore, history: RunHistory) -> None:
        self._run = run
        self._executor = StepExecutor(run, logs)
        self._history = history

    def run(self, pipeline: Pipeline) -> RunOutcome:
        """Run checkout, the stages and the post actions; never raises for step failures."""
        run = self._run
        plan = StagePlan.create(run.recorder, run.job.run_id, pipeline)
        state = RunState()
        stages = StageRunner(run, self._executor, plan, state, self._history)
        base = Scope(builtin_env(run.job), params=default_params(pipeline), flag=FailureFlag())
        self._checkout(plan, base, state)
        scope = self._pipeline_scope(pipeline, base, state)
        self._main(pipeline, plan, stages, scope, state)
        outcome = self._main_outcome(state)
        if run.cancel.reason is not CancelReason.SHUTDOWN:
            outcome = self._post(pipeline, plan, scope, state, outcome)
        run.recorder.skip_pending(run.job.run_id)
        return self._final(outcome, state)

    def _checkout(self, plan: StagePlan, scope: Scope, state: RunState) -> None:
        recorder = self._run.recorder
        recorder.start_stage(plan.checkout_id)
        status = RunStatus.SUCCEEDED
        try:
            self._executor.run_step(CheckoutStep(), scope, plan.checkout_id)
        except RunCancelled:
            status = RunStatus.CANCELLED
        except StepFailed as error:
            state.fail(f"checkout failed: {error}")
            scope.flag.set()
            status = RunStatus.FAILED
        recorder.finish_stage(plan.checkout_id, status)

    def _pipeline_scope(self, pipeline: Pipeline, base: Scope, state: RunState) -> Scope:
        scope = apply_agent(base, pipeline.agent)
        if state.failed:
            return scope
        try:
            return apply_environment(self._run, pipeline.environment, scope)
        except StepFailed as error:
            state.fail(str(error))
            base.flag.set()
            return scope

    def _main(
        self,
        pipeline: Pipeline,
        plan: StagePlan,
        stages: StageRunner,
        scope: Scope,
        state: RunState,
    ) -> None:
        options = StageOptions.parse(pipeline.options)

        def attempt(inner: Scope) -> None:
            fresh = replace(inner, flag=FailureFlag())
            statuses = stages.run_stages(pipeline.stages, fresh, ())
            if RunStatus.FAILED in statuses:
                raise StepFailed("a stage failed")

        def retry(_number: int, _error: StepFailed) -> None:
            self._run.recorder.reset_stages(plan.all_stage_ids)
            state.reset()

        if state.failed:
            return
        try:
            options.apply(scope, attempt, retry)
        except StepFailed as error:
            state.fail(str(error))
        except RunCancelled:
            logger.info("run cancelled", extra={"run_id": self._run.job.run_id})

    def _main_outcome(self, state: RunState) -> RunStatus:
        if self._run.cancel.is_set():
            return RunStatus.CANCELLED
        return RunStatus.FAILED if state.failed else RunStatus.SUCCEEDED

    def _post(
        self, pipeline: Pipeline, plan: StagePlan, scope: Scope, state: RunState, outcome: RunStatus
    ) -> RunStatus:
        if pipeline.post is None or plan.post_id is None:
            return outcome
        run = self._run
        run.cancel.enter_post_phase()
        run.recorder.start_stage(plan.post_id)
        post = PostRunner(self._executor, state)
        ok = post.run(pipeline.post, scope, plan.post_id, outcome, lambda: self._changed(outcome))
        run.recorder.finish_stage(plan.post_id, RunStatus.SUCCEEDED if ok else RunStatus.FAILED)
        return outcome if ok or outcome is not RunStatus.SUCCEEDED else RunStatus.FAILED

    def _changed(self, outcome: RunStatus) -> bool:
        previous = self._history.previous_run_status()
        return previous is not None and previous is not outcome

    def _final(self, outcome: RunStatus, state: RunState) -> RunOutcome:
        if self._run.cancel.reason is CancelReason.SHUTDOWN:
            return RunOutcome(RunStatus.INTERRUPTED, SHUTDOWN_MESSAGE)
        if outcome is RunStatus.CANCELLED:
            return RunOutcome(RunStatus.CANCELLED, CANCELLED_MESSAGE)
        if outcome is RunStatus.FAILED:
            return RunOutcome(RunStatus.FAILED, state.message or "the run failed")
        return RunOutcome(RunStatus.SUCCEEDED, None)
