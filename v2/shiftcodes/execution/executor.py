"""Executes step calls: one step record, one log file and one result per call."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from shiftcodes.adapters.errors import AdapterError
from shiftcodes.domain.enums import RunStatus
from shiftcodes.execution.context import RunContext, StepContext, StepSite
from shiftcodes.execution.errors import RunCancelled, StepFailed
from shiftcodes.execution.logs import LogStore
from shiftcodes.execution.scope import Scope
from shiftcodes.execution.step_base import Step
from shiftcodes.execution.step_registry import STEP_CLASSES
from shiftcodes.pipeline.ast import StepCall

logger = logging.getLogger(__name__)


class StepExecutor:
    """Runs steps for one run; also the sequencer block steps use for their bodies."""

    def __init__(self, run: RunContext, logs: LogStore) -> None:
        self._run = run
        self._logs = logs

    def run_all(self, steps: Sequence[StepCall], scope: Scope, stage_id: int) -> None:
        """Execute ``steps`` in order; the first failure stops the sequence."""
        for call in steps:
            self.run_one(call, scope, stage_id)

    def run_one(self, call: StepCall, scope: Scope, stage_id: int) -> None:
        """Execute one step call, recording its status, exit code and log."""
        if not scope.post and self._run.cancel.is_set():
            raise RunCancelled
        step_class = STEP_CLASSES.get(call.name)
        if step_class is None:
            raise StepFailed(f"unknown step '{call.name}'")
        self.run_step(step_class(call), scope, stage_id)

    def run_step(self, step: Step, scope: Scope, stage_id: int) -> None:
        """Execute a step instance (also used for the built-in checkout)."""
        call = step.call
        label = self._run.redactor.redact(step.label())
        step_id = self._run.recorder.start_step(stage_id, call.name, label)
        run_id = self._run.job.run_id
        log = self._logs.open_writer(
            run_id, step_id, self._run.redactor, self._run.config.log_max_bytes
        )
        ctx = StepContext(self._run, scope, StepSite(stage_id, step_id), log, self)
        status = RunStatus.FAILED
        try:
            ctx.checkpoint()
            step.execute(ctx)
            status = RunStatus.SUCCEEDED
        except RunCancelled:
            status = RunStatus.CANCELLED
            raise
        except (StepFailed, AdapterError) as error:
            if not scope.post and self._run.cancel.is_set():
                # a killed container exits non-zero; during a cancel that is not a failure
                status = RunStatus.CANCELLED
                raise RunCancelled from error
            ctx.line(f"ERROR: {error}")
            if isinstance(error, StepFailed):
                raise
            raise StepFailed(str(error)) from error
        except Exception as error:
            logger.exception("step crashed", extra={"run_id": run_id, "step": call.name})
            ctx.line("ERROR: internal error (see the worker log)")
            raise StepFailed("internal error") from error
        finally:
            log.close()
            self._run.recorder.finish_step(step_id, status, ctx.exit_code)
