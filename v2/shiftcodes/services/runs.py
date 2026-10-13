"""Run creation use case (numbering and snapshotting)."""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError

from shiftcodes.domain.clock import Clock, utcnow
from shiftcodes.domain.enums import EnvironmentName, PipelineSource, RunStatus, RunTrigger
from shiftcodes.domain.errors import ConflictError
from shiftcodes.domain.values import BranchName, CommitSha
from shiftcodes.repositories.database import UnitOfWork
from shiftcodes.repositories.models import Run
from shiftcodes.services.lookup import require_environment


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
    ) -> Run:
        """Queue a run numbered ``last + 1`` within its environment.

        Concurrent creators racing for the same number hit the unique constraint and
        receive ``ConflictError``; the caller may retry.
        """
        try:
            with self._uow:
                environment = require_environment(self._uow, slug, name)
                run = Run(
                    environment_id=environment.id,
                    number=self._uow.runs.last_number(environment.id) + 1,
                    trigger=trigger,
                    branch=str(BranchName(branch)),
                    commit_sha=str(CommitSha(commit_sha)),
                    pipeline_source=pipeline_source,
                    pipeline_text=pipeline_text,
                    status=RunStatus.QUEUED,
                    triggered_by=triggered_by,
                    created_at=self._clock(),
                )
                return self._uow.runs.add(run)
        except IntegrityError as error:
            raise ConflictError("run number was taken concurrently") from error
