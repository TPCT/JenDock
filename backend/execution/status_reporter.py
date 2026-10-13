"""Optional commit status reporting. A failure here is logged and never fails a run."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

from backend.adapters.commit_status import CommitState, CommitStatus
from backend.domain.enums import RunStatus
from backend.domain.errors import NotFoundError
from backend.execution.context import RunJob
from backend.services.secrets import SecretService

logger = logging.getLogger(__name__)
_FINAL_STATES = {
    RunStatus.SUCCEEDED: CommitState.SUCCESS,
    RunStatus.FAILED: CommitState.FAILURE,
}


class StatusPublisher(Protocol):
    """Publishes one commit status with the given token."""

    def publish(self, status: CommitStatus, token: str) -> None:
        """Raise on failure."""


class StatusReporter(Protocol):
    """What the executor calls around a run."""

    def started(self, job: RunJob) -> None:
        """The run began executing."""

    def finished(self, job: RunJob, status: RunStatus, message: str | None) -> None:
        """The run reached its final ``status``."""


class NoStatusReporter:
    """Reporting is switched off (the default)."""

    def started(self, job: RunJob) -> None:
        """Do nothing."""

    def finished(self, job: RunJob, status: RunStatus, message: str | None) -> None:
        """Do nothing."""


class CommitStatusReporter:
    """Publishes ``pending`` at start and the final state at the end.

    The token is the environment secret ``token_secret`` (resolved, and so audited, through the
    secret store). When it is not stored the run is simply not reported.
    """

    def __init__(
        self,
        publisher: StatusPublisher,
        secret_service: Callable[[], SecretService],
        token_secret: str,
        public_origin: str,
    ) -> None:
        self._publisher = publisher
        self._secret_service = secret_service
        self._token_secret = token_secret
        self._origin = public_origin.rstrip("/")

    def started(self, job: RunJob) -> None:
        """Publish ``pending``."""
        self._publish(job, CommitState.PENDING, f"Run #{job.number} is running")

    def finished(self, job: RunJob, status: RunStatus, message: str | None) -> None:
        """Publish ``success``, ``failure`` or ``error`` (cancelled and interrupted runs)."""
        state = _FINAL_STATES.get(status, CommitState.ERROR)
        detail = f": {message}" if message and status is not RunStatus.SUCCEEDED else ""
        self._publish(job, state, f"Run #{job.number} {status.value}{detail}")

    def _publish(self, job: RunJob, state: CommitState, description: str) -> None:
        status = CommitStatus(
            repository=job.repository,
            commit_sha=job.commit_sha,
            state=state,
            description=" ".join(description.split()),
            context=f"jendock/{job.environment}",
            target_url=f"{self._origin}/runs/{job.run_id}",
        )
        try:
            token = self._secret_service().resolve_for_run(
                job.environment_id, [self._token_secret], job.run_id
            )[self._token_secret]
            self._publisher.publish(status, token)
        except NotFoundError:
            logger.info(
                "commit status skipped: token secret not stored", extra={"run_id": job.run_id}
            )
        except Exception as error:
            # Only the exception type: messages may quote a response, never the token.
            logger.warning(
                "commit status failed",
                extra={"run_id": job.run_id, "error": type(error).__name__},
            )
