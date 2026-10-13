"""Steps that touch the outside world: ``deploy`` and ``healthCheck``."""

from __future__ import annotations

import time

from shiftcodes.adapters.deployer import DeployRequest, ServiceImage
from shiftcodes.adapters.errors import AdapterError
from shiftcodes.execution.context import StepContext
from shiftcodes.execution.errors import DeadlineExceeded, RunCancelled, StepFailed
from shiftcodes.execution.scope import describe
from shiftcodes.execution.step_base import Step

ATTEMPT_TIMEOUT = 10.0


class DeployStep(Step):
    """``deploy(service: 'web', image: 'logical')`` or ``deploy(services: [web: 'logical'])``."""

    name = "deploy"

    def _wanted(self, ctx: StepContext) -> dict[str, str]:
        wanted = {name: ctx.text(image) for name, image in self.mapping("services").items()}
        service, image = self.string("service"), self.string("image")
        if service is not None and image is not None:
            wanted[ctx.text(service)] = ctx.text(image)
        return wanted

    def summary(self) -> str:
        """The services as written."""
        service = self.string("service")
        if service is not None:
            return describe(service)
        return ", ".join(self.mapping("services"))

    def execute(self, ctx: StepContext) -> None:
        """Hand the built images to the deployer."""
        job = ctx.run.job
        images = []
        for service, logical in self._wanted(ctx).items():
            tag = ctx.run.images.tag_of(logical)
            if tag is None:
                raise StepFailed(f"image '{logical}' was not built in this run")
            images.append(ServiceImage(service, tag))
        request = DeployRequest(
            project=job.project,
            environment=job.environment,
            compose_project=job.compose_project,
            compose_files=job.compose_files,
            services=tuple(images),
            run_id=job.run_id,
            commit_sha=job.commit_sha,
        )
        ctx.run.deployer.deploy(request, ctx.line, ctx.cancel_signal)


class HealthCheckStep(Step):
    """``healthCheck(url: '...', expectStatus: 200, timeout: 60)``: GET until it answers."""

    name = "healthCheck"

    def summary(self) -> str:
        """The URL as written."""
        return describe(self.required_string("url"))

    def execute(self, ctx: StepContext) -> None:
        """Poll the URL until it returns the expected status or the time is up."""
        url = ctx.text(self.required_string("url"))
        expected = self.integer("expectStatus", 200)
        limit = time.monotonic() + self.integer("timeout", 60)
        last = "no attempt was made"
        while True:
            ctx.checkpoint()
            last = self._attempt(ctx, url, expected, limit)
            if last == "":
                return
            ctx.line(last)
            if time.monotonic() >= limit:
                raise DeadlineExceeded(f"health check failed: {last}")
            self._pause(ctx, limit)

    def _attempt(self, ctx: StepContext, url: str, expected: int, limit: float) -> str:
        budget = min(ATTEMPT_TIMEOUT, max(1.0, limit - time.monotonic()))
        try:
            status = ctx.run.probe.get_status(url, budget)
        except AdapterError as error:
            return str(error)
        if status == expected:
            ctx.line(f"health check passed: {status}")
            return ""
        return f"got status {status}, expected {expected}"

    def _pause(self, ctx: StepContext, limit: float) -> None:
        wait = min(ctx.run.config.health_interval, max(0.0, limit - time.monotonic()))
        if not ctx.scope.post and ctx.run.cancel.wait(wait):
            raise RunCancelled
        if ctx.scope.post:
            time.sleep(wait)
