"""Run creation use case (numbering and snapshotting)."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.exc import IntegrityError

from shiftcodes.domain.actor import Actor
from shiftcodes.domain.audit import AuditEntry, AuditTarget
from shiftcodes.domain.clock import Clock, utcnow
from shiftcodes.domain.enums import EnvironmentName, PipelineSource, RunStatus, RunTrigger
from shiftcodes.domain.errors import ConflictError
from shiftcodes.domain.values import BranchName, CommitSha
from shiftcodes.repositories.database import UnitOfWork
from shiftcodes.repositories.models import Environment, Run
from shiftcodes.services.audit import add_audit_event
from shiftcodes.services.lookup import require_environment


@dataclass(frozen=True, slots=True)
class RunRequest:
    """What a new queued run records.

    ``pipeline_text`` of ``None`` means the pipeline is resolved when the worker starts the
    run (a repository ``Jenkinsfile`` wins): the row then holds the environment's latest
    stored pipeline as a provisional snapshot.
    """

    trigger: RunTrigger
    branch: str
    commit_sha: str
    pipeline_source: PipelineSource = PipelineSource.PLATFORM
    pipeline_text: str | None = None
    triggered_by: int | None = None
    triggered_by_token_id: int | None = None
    # Audit the creation as ``run.create`` by this actor, in the same transaction.
    audit_actor: Actor | None = None
    audit_details: dict[str, object] = field(default_factory=dict)


def queue_run(uow: UnitOfWork, clock: Clock, environment: Environment, request: RunRequest) -> Run:
    """Add a queued run numbered ``last + 1`` in the active unit of work."""
    text = request.pipeline_text
    if text is None:
        stored = uow.pipelines.get_latest(environment.id)
        text = stored.jenkinsfile if stored else ""
    run = Run(
        environment_id=environment.id,
        number=uow.runs.last_number(environment.id) + 1,
        trigger=request.trigger,
        branch=str(BranchName(request.branch)),
        commit_sha=str(CommitSha(request.commit_sha)),
        pipeline_source=request.pipeline_source,
        pipeline_text=text,
        status=RunStatus.QUEUED,
        triggered_by=request.triggered_by,
        triggered_by_token_id=request.triggered_by_token_id,
        created_at=clock(),
    )
    uow.runs.add(run)
    if request.audit_actor is not None:
        details: dict[str, object] = {
            "number": run.number,
            "environment": environment.name.value,
            "trigger": run.trigger.value,
            "branch": run.branch,
            "commit": run.commit_sha,
            **request.audit_details,
        }
        entry = AuditEntry(
            request.audit_actor, "run.create", AuditTarget("run", str(run.id)), details
        )
        add_audit_event(uow, entry, clock)
    return run


class RunService:
    """Creates queued runs with per-environment sequential numbers."""

    def __init__(self, uow: UnitOfWork, clock: Clock = utcnow) -> None:
        self._uow = uow
        self._clock = clock

    def create_queued(  # noqa: PLR0913 - a run snapshot genuinely has these inputs
        self,
        slug: str,
        name: EnvironmentName,
        *,
        trigger: RunTrigger,
        branch: str,
        commit_sha: str,
        pipeline_source: PipelineSource,
        pipeline_text: str,
        triggered_by: int | None = None,
        triggered_by_token_id: int | None = None,
    ) -> Run:
        """Queue a run numbered ``last + 1`` within its environment.

        Concurrent creators racing for the same number hit the unique constraint and
        receive ``ConflictError``; the caller may retry.
        """
        request = RunRequest(
            trigger,
            branch,
            commit_sha,
            pipeline_source,
            pipeline_text,
            triggered_by,
            triggered_by_token_id,
        )
        try:
            with self._uow:
                environment = require_environment(self._uow, slug, name)
                return queue_run(self._uow, self._clock, environment, request)
        except IntegrityError as error:
            raise ConflictError("run number was taken concurrently") from error
