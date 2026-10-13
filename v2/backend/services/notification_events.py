"""Building notification events at the points where things happen.

Each notifier loads what it needs by id, builds a bounded ``EventPayload`` and hands it to the
sink. Everything is best effort: ``safely`` swallows and logs any failure, so a notification
problem can never fail a run, a deployment or a renewal.

The only log content that ever leaves is the last 20 lines of the failing step's log, which the
run's redactor already masked when the line was written; ``tail_of`` strips terminal escapes,
passes it through the worker's redactor once more and cuts every line.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from functools import partial
from typing import Protocol

from sqlalchemy import select

from backend.adapters.deploy_ledger import DeploymentLedger, ServiceChange
from backend.domain.enums import DeploymentStatus, RunStatus, RunTrigger
from backend.domain.notifications import (
    LOG_TAIL_LINES,
    MAX_LINE,
    EventPayload,
    NotificationEvent,
)
from backend.execution.logs import LogStore
from backend.logging import SecretRedactor
from backend.repositories.database import UnitOfWork
from backend.repositories.models import Deployment, Environment, Project, Run
from backend.services.certificates import CertificateStatus, RenewalOutcome
from backend.services.notification_outbox import EventSink

logger = logging.getLogger(__name__)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
TAIL_BYTES = 16 * 1024
CERTIFICATE_WARNING_DAYS = 14

_RUN_EVENTS = {
    RunStatus.FAILED: NotificationEvent.RUN_FAILED,
    RunStatus.INTERRUPTED: NotificationEvent.RUN_INTERRUPTED,
}
_DEPLOYMENT_EVENTS = {
    DeploymentStatus.SUCCEEDED: NotificationEvent.DEPLOYMENT_SUCCEEDED,
    DeploymentStatus.ROLLED_BACK: NotificationEvent.DEPLOYMENT_ROLLED_BACK,
    DeploymentStatus.ROLLBACK_FAILED: NotificationEvent.DEPLOYMENT_ROLLBACK_FAILED,
}
_DEPLOYMENT_TITLES = {
    DeploymentStatus.SUCCEEDED: "Deployment succeeded",
    DeploymentStatus.ROLLED_BACK: "Deployment rolled back",
    DeploymentStatus.ROLLBACK_FAILED: "Rollback failed, manual intervention needed",
}


class RunNotifications(Protocol):
    """What the executor calls when a run reaches its final status."""

    def run_finished(self, run_id: int, status: RunStatus) -> None:
        """Announce the outcome if it is notable; never raises."""


class NoRunNotifications:
    """Notifications are off (the default of the execution environment)."""

    def run_finished(self, run_id: int, status: RunStatus) -> None:
        """Do nothing."""


def safely(action: Callable[[], object], what: str) -> None:
    """Run ``action``; log and swallow anything it raises."""
    try:
        action()
    except Exception:
        logger.exception("notification event failed", extra={"what": what})


def tail_of(lines: Sequence[str], redactor: SecretRedactor | None) -> tuple[str, ...]:
    """The last 20 non-empty lines, escape-free, re-masked and cut."""
    cleaned: list[str] = []
    for raw in lines:
        text = _CONTROL.sub("", _ANSI.sub("", raw)).rstrip()
        if redactor is not None:
            text = redactor.redact_text(text)
        if text.strip():
            cleaned.append(text[:MAX_LINE])
    return tuple(cleaned[-LOG_TAIL_LINES:])


class RunNotifier:
    """Builds the run events (failed, fixed, interrupted) and emits them."""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        logs: LogStore,
        sink: EventSink,
        redactor: SecretRedactor | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._logs = logs
        self._sink = sink
        self._redactor = redactor

    def run_finished(self, run_id: int, status: RunStatus) -> None:
        """Emit ``run.failed``, ``run.interrupted`` or (after a failure) ``run.fixed``."""
        safely(lambda: self._finished(run_id, status), "run")

    def _finished(self, run_id: int, status: RunStatus) -> None:
        payload = self._build(run_id, status)
        if payload is not None:
            self._sink.emit(payload)

    def _build(self, run_id: int, status: RunStatus) -> EventPayload | None:
        with self._uow_factory() as uow:
            run = uow.runs.get(run_id)
            environment = uow.environments.get(run.environment_id) if run else None
            project = uow.projects.get(environment.project_id) if environment else None
            if run is None or environment is None or project is None:
                return None
            event = _RUN_EVENTS.get(status)
            if status is RunStatus.SUCCEEDED:
                if not self._follows_failure(uow, run):
                    return None
                event = NotificationEvent.RUN_FIXED
            if event is None:
                return None
            stage: str | None = None
            step: str | None = None
            tail: tuple[str, ...] = ()
            if status is RunStatus.FAILED:
                stage, step, tail = self._failure(uow, run)
            verb = {
                NotificationEvent.RUN_FAILED: "failed",
                NotificationEvent.RUN_FIXED: "succeeded again",
                NotificationEvent.RUN_INTERRUPTED: "was interrupted",
            }[event]
            return EventPayload(
                event=event,
                dedupe_key=f"{event.value}:{environment.id}",
                title=f"Run #{run.number} {verb}: {project.slug}/{environment.name.value}",
                project=project.slug,
                environment=environment.name.value,
                link_path=f"/runs/{run.id}",
                run_id=run.id,
                run_number=run.number,
                commit_sha=run.commit_sha,
                triggered_by=_trigger_label(uow, run),
                failed_stage=stage,
                failed_step=step,
                message=self._mask(run.message) if status is not RunStatus.SUCCEEDED else None,
                log_tail=tail,
                details={"Branch": run.branch},
            )

    def _mask(self, text: str | None) -> str | None:
        if text is None or self._redactor is None:
            return text
        return self._redactor.redact_text(text)

    @staticmethod
    def _follows_failure(uow: UnitOfWork, run: Run) -> bool:
        """Whether the latest run of the environment that ended succeeded or failed (not
        cancelled or interrupted) before this one failed."""
        query = (
            select(Run.status)
            .where(
                Run.environment_id == run.environment_id,
                Run.number < run.number,
                Run.status.in_([RunStatus.SUCCEEDED, RunStatus.FAILED]),
            )
            .order_by(Run.number.desc())
            .limit(1)
        )
        return uow.session.scalar(query) is RunStatus.FAILED

    def _failure(self, uow: UnitOfWork, run: Run) -> tuple[str | None, str | None, tuple[str, ...]]:
        """The first failed stage, its first failed step and that step's log tail."""
        for stage in uow.runs.list_stages(run.id):
            if stage.status is not RunStatus.FAILED:
                continue
            for step in uow.runs.list_steps(stage.id):
                if step.status is RunStatus.FAILED:
                    return stage.name, step.label, self._tail(run.id, step.id)
            return stage.name, None, ()
        return None, None, ()

    def _tail(self, run_id: int, step_id: int) -> tuple[str, ...]:
        size = self._logs.read_chunk(run_id, step_id, 0, 1).size
        offset = max(0, size - TAIL_BYTES)
        chunk = self._logs.read_chunk(run_id, step_id, offset, TAIL_BYTES)
        lines = chunk.text.splitlines()
        if offset > 0 and lines:
            lines = lines[1:]  # the first line was cut in the middle
        return tail_of(lines, self._redactor)


def _trigger_label(uow: UnitOfWork, run: Run) -> str:
    if run.triggered_by is not None:
        user = uow.users.get(run.triggered_by)
        if user is not None:
            return f"{user.username} ({run.trigger.value})"
    if run.triggered_by_token_id is not None:
        token = uow.tokens.get(run.triggered_by_token_id)
        if token is not None:
            return f"API token {token.name} ({run.trigger.value})"
    if run.trigger is RunTrigger.PUSH:
        return "GitHub push"
    return run.trigger.value


class DeploymentNotifier:
    """Builds the deployment events (succeeded, rolled back, rollback failed)."""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        sink: EventSink,
        redactor: SecretRedactor | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._sink = sink
        self._redactor = redactor

    def deployment_finished(self, deployment_id: int) -> None:
        """Emit the event of the deployment's final status; never raises."""
        safely(lambda: self._finished(deployment_id), "deployment")

    def _finished(self, deployment_id: int) -> None:
        payload = self._build(deployment_id)
        if payload is not None:
            self._sink.emit(payload)

    def _build(self, deployment_id: int) -> EventPayload | None:
        with self._uow_factory() as uow:
            deployment = uow.deployments.get(deployment_id)
            event = _DEPLOYMENT_EVENTS.get(deployment.status) if deployment else None
            if deployment is None or event is None:
                return None
            environment = uow.environments.get(deployment.environment_id)
            project = uow.projects.get(environment.project_id) if environment else None
            run = uow.runs.get(deployment.run_id)
            if environment is None or project is None:
                return None
            return _deployment_payload(uow, deployment, event, project, environment, run)


def _deployment_payload(  # noqa: PLR0913, PLR0917
    uow: UnitOfWork,
    deployment: Deployment,
    event: NotificationEvent,
    project: Project,
    environment: Environment,
    run: Run | None,
) -> EventPayload:
    services = ", ".join(str(item.get("service")) for item in deployment.services if item)
    failed = deployment.status is not DeploymentStatus.SUCCEEDED
    title = _DEPLOYMENT_TITLES[deployment.status]
    return EventPayload(
        event=event,
        dedupe_key=f"{event.value}:{environment.id}",
        title=f"{title}: {project.slug}/{environment.name.value}",
        project=project.slug,
        environment=environment.name.value,
        link_path=f"/runs/{deployment.run_id}",
        run_id=deployment.run_id,
        run_number=run.number if run else None,
        deployment_id=deployment.id,
        commit_sha=deployment.commit_sha,
        triggered_by=_trigger_label(uow, run) if run else None,
        message=deployment.message if failed else None,
        details={"Services": services} if services else {},
    )


class NotifyingLedger:
    """A ``DeploymentLedger`` that announces final deployment statuses after recording them.

    Wraps the database ledger, so every path that finishes a deployment (a normal deploy, a
    rollback, crash recovery) is covered by one hook.
    """

    def __init__(self, inner: DeploymentLedger, notifier: DeploymentNotifier) -> None:
        self._inner = inner
        self._notifier = notifier

    def begin(self, project: str, environment: str, run_id: int, commit_sha: str) -> int:
        """Delegate."""
        return self._inner.begin(project, environment, run_id, commit_sha)

    def record_services(self, deployment_id: int, changes: Sequence[ServiceChange]) -> None:
        """Delegate."""
        self._inner.record_services(deployment_id, changes)

    def finish(self, deployment_id: int, status: DeploymentStatus, message: str | None) -> None:
        """Record the status, then announce it."""
        self._inner.finish(deployment_id, status, message)
        self._notifier.deployment_finished(deployment_id)

    def fail_unfinished(self, message: str) -> list[int]:
        """Delegate (a failed deployment changed nothing, so it is not announced)."""
        return self._inner.fail_unfinished(message)

    def protected_images(self, project: str, environment: str) -> frozenset[str]:
        """Delegate."""
        return self._inner.protected_images(project, environment)


class CertificateNotifier:
    """Builds certificate events for the renewal loop."""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        sink: EventSink,
        redactor: SecretRedactor | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._sink = sink
        self._redactor = redactor

    def after_pass(
        self, outcomes: Sequence[RenewalOutcome], statuses: Sequence[CertificateStatus]
    ) -> None:
        """Announce failed renewals and certificates with fewer than 14 days left."""
        for outcome in outcomes:
            if not outcome.ok and outcome.message != "skipped":
                safely(partial(self._renewal_failed, outcome), "certificate")
        for status in statuses:
            if status.days_remaining < CERTIFICATE_WARNING_DAYS:
                safely(partial(self._expiring, status), "certificate")

    def _renewal_failed(self, outcome: RenewalOutcome) -> None:
        message = outcome.message
        if self._redactor is not None:
            message = self._redactor.redact_text(message)
        self._emit(
            NotificationEvent.CERTIFICATE_RENEWAL_FAILED,
            outcome.domain_id,
            f"Certificate renewal failed: {outcome.hostname}",
            message,
            {"Hostname": outcome.hostname},
        )

    def _expiring(self, status: CertificateStatus) -> None:
        days = max(status.days_remaining, 0)
        self._emit(
            NotificationEvent.CERTIFICATE_EXPIRING,
            status.domain_id,
            f"Certificate for {status.hostname} expires in {days} days",
            None,
            {
                "Hostname": status.hostname,
                "Expires": status.not_after.date().isoformat(),
                "Days left": str(days),
            },
        )

    def _emit(
        self,
        event: NotificationEvent,
        domain_id: int,
        title: str,
        message: str | None,
        details: dict[str, str],
    ) -> None:
        with self._uow_factory() as uow:
            domain = uow.domains.get(domain_id)
            environment = uow.environments.get(domain.environment_id) if domain else None
            project = uow.projects.get(environment.project_id) if environment else None
            if environment is None or project is None:
                return
            slug, env = project.slug, environment.name.value
        self._sink.emit(
            EventPayload(
                event=event,
                dedupe_key=f"{event.value}:{domain_id}",
                title=title,
                project=slug,
                environment=env,
                link_path=f"/projects/{slug}/environments/{env}/domains",
                message=message,
                details=details,
            )
        )
