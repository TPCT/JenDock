"""Choosing the pipeline of a run: the repository's ``Jenkinsfile`` or the stored one."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from shiftcodes.adapters.errors import SourceError
from shiftcodes.adapters.sources import CheckoutRequest, SourceProvider
from shiftcodes.domain.enums import PipelineSource
from shiftcodes.execution.context import RunJob

JENKINSFILE_PATH = "Jenkinsfile"
SHORT_SHA = 12
StoredPipeline = Callable[[int], str | None]


class PipelineUnavailableError(Exception):
    """No pipeline could be chosen; the message is shown to the user and logged on the run."""


@dataclass(frozen=True, slots=True)
class ResolvedPipeline:
    """The chosen pipeline text, where it came from and a line for the run log."""

    source: PipelineSource
    text: str
    note: str


class PipelineResolver:
    """``Jenkinsfile`` at the pinned commit wins; otherwise the environment's latest stored
    pipeline; otherwise the run cannot start.

    A repository that cannot be read is an error, never a silent fall back to the stored
    pipeline: running something other than what the repository defines would be surprising.
    """

    def __init__(self, source: SourceProvider, stored: StoredPipeline) -> None:
        self._source = source
        self._stored = stored

    def resolve(self, job: RunJob) -> ResolvedPipeline:
        """Pick the pipeline for ``job``; raises ``PipelineUnavailableError``."""
        sha = job.commit_sha[:SHORT_SHA]
        request = CheckoutRequest(job.repository, job.commit_sha, job.git_transport)
        try:
            text = self._source.read_file(request, JENKINSFILE_PATH)
        except SourceError as error:
            raise PipelineUnavailableError(
                f"could not read the Jenkinsfile at {sha}: {error}"
            ) from error
        if text is not None:
            note = f"using the Jenkinsfile of the repository at {sha}"
            return ResolvedPipeline(PipelineSource.REPOSITORY, text, note)
        return self._platform(job, sha)

    def _platform(self, job: RunJob, sha: str) -> ResolvedPipeline:
        text = self._stored(job.environment_id)
        note = (
            f"no Jenkinsfile in the repository at {sha}; using the pipeline stored in the platform"
        )
        if text is None and job.pipeline_text.strip():
            # The snapshot taken when the run was queued is the latest stored version of then.
            text, note = job.pipeline_text, note + " (as of when the run was queued)"
        if text is None:
            raise PipelineUnavailableError(
                f"no Jenkinsfile in the repository at {sha} and no pipeline is stored for "
                f"{job.project}/{job.environment}: add a Jenkinsfile to the repository or "
                "save a pipeline in the platform"
            )
        return ResolvedPipeline(PipelineSource.PLATFORM, text, note)
