"""Executes one claimed run from start to final status."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace

from shiftcodes.domain.diagnostics import Diagnostic
from shiftcodes.domain.enums import RunStatus
from shiftcodes.domain.errors import NotFoundError
from shiftcodes.execution.cancel import CancelReason, CancelToken
from shiftcodes.execution.context import ImageBook, RunContext, RunJob
from shiftcodes.execution.docker_agent import RUN_ID_LABEL
from shiftcodes.execution.environment import ExecutionEnv
from shiftcodes.execution.history import LifecycleHistory
from shiftcodes.execution.logs import RunRedactor
from shiftcodes.execution.pipeline_resolver import (
    PipelineResolver,
    PipelineUnavailableError,
    ResolvedPipeline,
)
from shiftcodes.execution.runner import PipelineRunner, RunOutcome
from shiftcodes.pipeline import PipelineSyntaxError, ValidationContext, parse, validate
from shiftcodes.pipeline.ast import Pipeline
from shiftcodes.pipeline.errors import Severity

logger = logging.getLogger(__name__)
MAX_SYNTAX_MESSAGE = 300
PIPELINE_STAGE = "Pipeline"
PIPELINE_STEP_LABEL = "Choose and validate the pipeline"


class CancelWatcher(threading.Thread):
    """Polls the database for a cancel request and stops the run's containers.

    While the main phase is being cancelled it kills containers by label on every poll, which
    also catches a container that was started in the instant between two polls.
    """

    def __init__(self, env: ExecutionEnv, run_id: int, token: CancelToken) -> None:
        super().__init__(name=f"cancel-watch-{run_id}", daemon=True)
        self._env = env
        self._run_id = run_id
        self._token = token
        self._stopped = threading.Event()

    def run(self) -> None:
        lifecycle = self._env.lifecycle()
        interval = self._env.config.cancel_poll_interval
        while not self._stopped.wait(interval):
            try:
                if not self._token.is_set() and lifecycle.is_cancel_requested(self._run_id):
                    self._token.cancel(CancelReason.CANCELLED)
                if self._token.interrupting:
                    self._env.runtime.kill_by_label(f"{RUN_ID_LABEL}={self._run_id}")
            except Exception:
                logger.exception("cancel watcher failed", extra={"run_id": self._run_id})

    def stop(self) -> None:
        """Stop polling."""
        self._stopped.set()
        self.join(5)


class RunExecutor:
    """Prepares the workspace, runs the pipeline and records the final status."""

    def __init__(self, env: ExecutionEnv) -> None:
        self._env = env

    def execute(self, run_id: int, token: CancelToken) -> RunOutcome:
        """Run ``run_id`` (already marked running) and finish it; always cleans up."""
        env = self._env
        watcher = CancelWatcher(env, run_id, token)
        watcher.start()
        try:
            outcome, job = self._guarded(run_id, token)
        finally:
            watcher.stop()
            self._cleanup(run_id)
        env.lifecycle().finish(run_id, outcome.status, outcome.message)
        if job is not None:
            self._report(lambda: env.status.finished(job, outcome.status, outcome.message))
        return outcome

    def _guarded(self, run_id: int, token: CancelToken) -> tuple[RunOutcome, RunJob | None]:
        job: RunJob | None = None
        try:
            job, secret_names = self._load(run_id)
            loaded = job
            self._report(lambda: self._env.status.started(loaded))
            resolved = self._resolve(job)
            if isinstance(resolved, RunOutcome):
                return resolved, job
            job = replace(job, pipeline_text=resolved.text, pipeline_source=resolved.source)
            self._store_snapshot(job)
            pipeline = self._parse(job, secret_names)
            if isinstance(pipeline, RunOutcome):
                return pipeline, job
            return self._run(job, pipeline, token), job
        except Exception:
            logger.exception("run crashed", extra={"run_id": run_id})
            return RunOutcome(RunStatus.FAILED, "internal error while executing the run"), job

    @staticmethod
    def _report(action: Callable[[], None]) -> None:
        """Commit status reporting must never fail a run, whatever the reporter does."""
        try:
            action()
        except Exception:
            logger.exception("status reporter failed")

    def _cleanup(self, run_id: int) -> None:
        try:
            self._env.runtime.kill_by_label(f"{RUN_ID_LABEL}={run_id}")
        except Exception:
            logger.exception("container cleanup failed", extra={"run_id": run_id})
        self._env.workspaces.remove(run_id)

    def _load(self, run_id: int) -> tuple[RunJob, frozenset[str]]:
        with self._env.uow() as uow:
            run = uow.runs.get(run_id)
            environment = uow.environments.get(run.environment_id) if run else None
            project = uow.projects.get(environment.project_id) if environment else None
            if run is None or environment is None or project is None:
                raise NotFoundError(f"run not found: {run_id}")
            names = frozenset(s.name for s in uow.secrets.list_for_environment(environment.id))
            job = RunJob(
                run_id=run.id,
                number=run.number,
                project=project.slug,
                environment=environment.name.value,
                environment_id=environment.id,
                branch=run.branch,
                commit_sha=run.commit_sha,
                repository=project.repository,
                compose_project=environment.compose_project,
                compose_files=tuple(environment.compose_files),
                services=tuple(environment.services),
                pipeline_text=run.pipeline_text,
                git_transport=project.git_transport,
                pipeline_source=run.pipeline_source,
            )
            return job, names

    def _resolve(self, job: RunJob) -> ResolvedPipeline | RunOutcome:
        """Pick the pipeline: the repository's Jenkinsfile at the commit, else the stored one."""
        env = self._env

        def stored(environment_id: int) -> str | None:
            with env.uow() as uow:
                latest = uow.pipelines.get_latest(environment_id)
                return latest.jenkinsfile if latest else None

        try:
            return PipelineResolver(env.source, stored).resolve(job)
        except PipelineUnavailableError as error:
            return self._fail_with_log(job.run_id, [str(error)], str(error))

    def _store_snapshot(self, job: RunJob) -> None:
        """Record on the run which pipeline source and exact text it uses."""
        with self._env.uow() as uow:
            run = uow.runs.get(job.run_id)
            if run is None:
                raise NotFoundError(f"run not found: {job.run_id}")
            run.pipeline_source = job.pipeline_source
            run.pipeline_text = job.pipeline_text

    def _parse(self, job: RunJob, secret_names: frozenset[str]) -> Pipeline | RunOutcome:
        """Parse and validate the snapshot again: secrets may have been deleted since.

        Every diagnostic goes to the run log (a ``Pipeline`` stage), the run message names
        the first error.
        """
        try:
            pipeline = parse(job.pipeline_text)
        except PipelineSyntaxError as error:
            message = f"pipeline syntax error: {error}"
            return self._fail_with_log(job.run_id, [message], message)
        diagnostics = validate(pipeline, ValidationContext(secret_names))
        errors = [d for d in diagnostics if d.severity is Severity.ERROR]
        if errors:
            first = errors[0]
            message = f"pipeline is invalid (line {first.span.line}): {first.message}"
            lines = [_diagnostic_line(d) for d in diagnostics]
            return self._fail_with_log(job.run_id, lines, message)
        return pipeline

    def _fail_with_log(self, run_id: int, lines: Sequence[str], message: str) -> RunOutcome:
        """Record a failed ``Pipeline`` stage whose step log holds ``lines``."""
        env = self._env
        recorder = env.recorder()
        stage_id = recorder.plan_stages(run_id, [PIPELINE_STAGE])[0]
        recorder.start_stage(stage_id)
        step_id = recorder.start_step(stage_id, "pipeline", PIPELINE_STEP_LABEL)
        writer = env.logs.open_writer(run_id, step_id, RunRedactor(), env.config.log_max_bytes)
        try:
            for line in lines:
                writer.write_line(line)
        finally:
            writer.close()
        recorder.finish_step(step_id, RunStatus.FAILED, None)
        recorder.finish_stage(stage_id, RunStatus.FAILED)
        return RunOutcome(RunStatus.FAILED, message)

    def _run(self, job: RunJob, pipeline: Pipeline, token: CancelToken) -> RunOutcome:
        env = self._env
        workspace = env.workspaces.create(job.run_id)
        lifecycle = env.lifecycle()

        def resolve(names: Sequence[str]) -> Mapping[str, str]:
            return env.secret_service().resolve_for_run(job.environment_id, names, job.run_id)

        context = RunContext(
            job=job,
            config=env.config,
            runtime=env.runtime,
            deployer=env.deployer,
            probe=env.probe,
            source=env.source,
            recorder=env.recorder(),
            redactor=RunRedactor(),
            cancel=token,
            workspaces=env.workspaces,
            workspace=workspace,
            resolve_secrets=resolve,
            images=ImageBook(),
        )
        history = LifecycleHistory(lifecycle, job.run_id)
        return PipelineRunner(context, env.logs, history).run(pipeline)


def _diagnostic_line(diagnostic: Diagnostic) -> str:
    span = diagnostic.span
    return (
        f"{diagnostic.severity.value}: line {span.line}, column {span.column}: {diagnostic.message}"
    )
