"""The built-in checkout, recorded as the first stage of every run."""

from __future__ import annotations

from backend.adapters.sources import CheckoutRequest
from backend.execution.context import StepContext
from backend.execution.step_base import Step
from backend.pipeline.ast import StepCall
from backend.pipeline.errors import Span

SHA_LENGTH = 12


class CheckoutStep(Step):
    """Clones the repository at the pinned commit into the run's workspace."""

    name = "checkout"

    def __init__(self) -> None:
        super().__init__(StepCall("checkout", (), None, Span(0, 0)))

    def summary(self) -> str:
        """Nothing beyond the step name."""
        return "source at the pinned commit"

    def execute(self, ctx: StepContext) -> None:
        """Fetch the commit and check the workspace size."""
        job = ctx.run.job
        ctx.line(f"checking out {job.repository} at {job.commit_sha[:SHA_LENGTH]}")
        ctx.line(f"pipeline source: {job.pipeline_source.value}")
        request = CheckoutRequest(job.repository, job.commit_sha, job.git_transport)
        ctx.run.source.checkout(request, ctx.run.workspace)
        ctx.run.workspaces.enforce_limit(ctx.run.workspace)
        ctx.line("checkout complete")
