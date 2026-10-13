"""Steps that use the container runtime: ``sh`` and ``dockerBuild``."""

from __future__ import annotations

from pathlib import Path

from shiftcodes.adapters.container_runtime import BuildSpec
from shiftcodes.execution.context import StepContext
from shiftcodes.execution.docker_agent import (
    BUILD_RUN_LABEL,
    ContainerPlanner,
    limited_memory,
    raise_unless_ok,
)
from shiftcodes.execution.errors import StepFailed
from shiftcodes.execution.scope import describe
from shiftcodes.execution.step_base import Step

SHA_LENGTH = 12


class ShStep(Step):
    """``sh 'script'``: runs inside a container, never on the host."""

    name = "sh"

    def summary(self) -> str:
        """The step's ``label`` or the first line of its script, as written."""
        label = self.string("label")
        if label is not None:
            return describe(label)
        lines = [line for line in describe(self.required_string("script")).splitlines() if line]
        return lines[0] if lines else ""

    def execute(self, ctx: StepContext) -> None:
        """Run the script in the container of the agent in force."""
        agent = ctx.scope.agent
        if agent is None:
            raise StepFailed("sh runs inside a container: set agent { docker { image '...' } }")
        script = ctx.text(self.required_string("script"), for_shell=True)
        spec = ContainerPlanner(ctx).plan(agent, script)
        ctx.line(f"running in {spec.image}")
        result = ctx.run.runtime.run_container(
            spec, ctx.line, timeout=ctx.effective_timeout(), cancel=ctx.cancel_signal
        )
        ctx.exit_code = result.exit_code
        ctx.run.workspaces.enforce_limit(ctx.run.workspace)
        raise_unless_ok(result, "script")


def image_tag(project: str, environment: str, image: str, sha: str, number: int) -> str:
    """The tag of a logical image: ``sc/<project>-<env>-<image>:<sha12>-r<run number>``."""
    return f"sc/{project}-{environment}-{image}:{sha[:SHA_LENGTH]}-r{number}"


class DockerBuildStep(Step):
    """``dockerBuild(image: 'name', ...)``: builds a logical image for this run."""

    name = "dockerBuild"

    def summary(self) -> str:
        """The logical image name."""
        return describe(self.required_string("image"))

    def execute(self, ctx: StepContext) -> None:
        """Build the image, tag it for this run and remember it as the logical image."""
        job = ctx.run.job
        logical = ctx.text(self.required_string("image"))
        tag = image_tag(job.project, job.environment, logical, job.commit_sha, job.number)
        context = self._path(ctx, self._text(ctx, "context", "."))
        dockerfile = self._path(ctx, self._text(ctx, "dockerfile", "Dockerfile"))
        if not context.is_dir():
            raise StepFailed("build context is not a directory")
        if not dockerfile.is_file():
            raise StepFailed("Dockerfile not found")
        target = self.string("target")
        spec = BuildSpec(
            context=context,
            dockerfile=dockerfile,
            tag=tag,
            target=ctx.text(target) if target else None,
            build_args={k: ctx.text(v) for k, v in self.mapping("args").items()},
            labels={
                "shiftcodes.project": job.project,
                "shiftcodes.environment": job.environment,
                BUILD_RUN_LABEL: str(job.run_id),
                "shiftcodes.commit": job.commit_sha,
            },
            memory=limited_memory(None, ctx.run.config.build_memory),
        )
        ctx.line(f"building {tag}")
        result = ctx.run.runtime.build_image(
            spec, ctx.line, timeout=ctx.effective_timeout(), cancel=ctx.cancel_signal
        )
        raise_unless_ok(result, "docker build")
        ctx.run.images.built(logical, tag)
        ctx.line(f"built {tag}")

    def _text(self, ctx: StepContext, name: str, default: str) -> str:
        value = self.string(name)
        return ctx.text(value) if value is not None else default

    @staticmethod
    def _path(ctx: StepContext, relative: str) -> Path:
        workspace = ctx.run.workspace.resolve()
        target = (workspace / ctx.scope.directory / relative).resolve()
        if not target.is_relative_to(workspace):
            raise StepFailed(f"path escapes the workspace: {relative!r}")
        return target
