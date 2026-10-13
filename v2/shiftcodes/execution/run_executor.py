"""Executes one claimed run from start to final status."""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping, Sequence

from shiftcodes.domain.enums import RunStatus
from shiftcodes.domain.errors import NotFoundError
from shiftcodes.execution.cancel import CancelReason, CancelToken
from shiftcodes.execution.context import ImageBook, RunContext, RunJob
from shiftcodes.execution.docker_agent import RUN_ID_LABEL
from shiftcodes.execution.environment import ExecutionEnv
from shiftcodes.execution.history import LifecycleHistory
from shiftcodes.execution.logs import RunRedactor
from shiftcodes.execution.runner import PipelineRunner, RunOutcome
from shiftcodes.pipeline import PipelineSyntaxError, ValidationContext, parse, validate
from shiftcodes.pipeline.ast import Pipeline
from shiftcodes.pipeline.errors import Severity

logger = logging.getLogger(__name__)
MAX_SYNTAX_MESSAGE = 300


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
            outcome = self._guarded(run_id, token)
        finally:
            watcher.stop()
            self._cleanup(run_id)
        env.lifecycle().finish(run_id, outcome.status, outcome.message)
        return outcome

    def _guarded(self, run_id: int, token: CancelToken) -> RunOutcome:
        try:
            job, secret_names = self._load(run_id)
            pipeline = self._parse(job, secret_names)
            if isinstance(pipeline, RunOutcome):
                return pipeline
            return self._run(job, pipeline, token)
        except Exception:
            logger.exception("run crashed", extra={"run_id": run_id})
            return RunOutcome(RunStatus.FAILED, "internal error while executing the run")

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
            )
            return job, names

    @staticmethod
    def _parse(job: RunJob, secret_names: frozenset[str]) -> Pipeline | RunOutcome:
        """Parse and validate the snapshot again: secrets may have been deleted since."""
        try:
            pipeline = parse(job.pipeline_text)
        except PipelineSyntaxError as error:
            return RunOutcome(RunStatus.FAILED, f"pipeline syntax error: {error}")
        errors = [
            d
            for d in validate(pipeline, ValidationContext(secret_names))
            if d.severity is Severity.ERROR
        ]
        if errors:
            first = errors[0]
            where = f"line {first.span.line}"
            return RunOutcome(RunStatus.FAILED, f"pipeline is invalid ({where}): {first.message}")
        return pipeline

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
