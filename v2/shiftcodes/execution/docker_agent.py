"""Docker agent semantics: which image, mounts, network and limits a container step gets."""

from __future__ import annotations

import shlex
import uuid
from dataclasses import dataclass, field

from shiftcodes.adapters.container_runtime import ExecResult, Mount, RunSpec
from shiftcodes.domain.errors import ValidationError
from shiftcodes.domain.resources import parse_cpus, parse_memory
from shiftcodes.execution.context import StepContext
from shiftcodes.execution.errors import DeadlineExceeded, RunCancelled, StepFailed
from shiftcodes.pipeline.ast import AgentDocker
from shiftcodes.pipeline.docker import check_docker_args

WORKSPACE_TARGET = "/workspace"
DEFAULT_HOME = "/tmp"  # noqa: S108 - a path inside the container, not on the host
RUN_ID_LABEL = "shiftcodes.run_id"
STEP_ID_LABEL = "shiftcodes.step_id"
# Images record the run that built them under a name of their own: a container started from an
# image inherits its labels, and a deployed service must never carry RUN_ID_LABEL (the label
# the worker kills by when a run ends).
BUILD_RUN_LABEL = "shiftcodes.build_run"


@dataclass(frozen=True, slots=True)
class AgentArgs:
    """The allowlisted ``args`` of a docker agent."""

    network_none: bool = False
    env: dict[str, str] = field(default_factory=dict)
    memory: str | None = None
    cpus: str | None = None


def parse_agent_args(text: str | None) -> AgentArgs:
    """Parse the allowlisted agent ``args`` (the validator already rejected anything else)."""
    if not text:
        return AgentArgs()
    problem = check_docker_args(text)
    if problem:
        raise StepFailed(problem)
    tokens = shlex.split(text)
    network_none = False
    env: dict[str, str] = {}
    memory: str | None = None
    cpus: str | None = None
    index = 0
    while index < len(tokens):
        flag, equals, inline = tokens[index].partition("=")
        index += 1
        if equals and flag.startswith("--"):
            value = inline
        else:
            value = tokens[index]
            index += 1
        if flag == "--network":
            network_none = True
        elif flag == "-e":
            name, _, content = value.partition("=")
            env[name] = content
        elif flag == "--memory":
            memory = value
        else:
            cpus = value
    return AgentArgs(network_none, env, memory, cpus)


def limited_memory(requested: str | None, ceiling: str) -> str:
    """The memory limit to use: the request when it does not exceed the configured maximum."""
    if requested is None:
        return ceiling
    try:
        if parse_memory(requested) > parse_memory(ceiling):
            raise StepFailed(f"--memory {requested} exceeds the configured maximum of {ceiling}")
    except ValidationError as error:
        raise StepFailed(str(error)) from error
    return requested


def limited_cpus(requested: str | None, ceiling: float) -> float:
    """The CPU limit to use: the request when it does not exceed the configured maximum."""
    if requested is None:
        return ceiling
    try:
        value = parse_cpus(requested)
    except ValidationError as error:
        raise StepFailed(str(error)) from error
    if value > ceiling:
        raise StepFailed(f"--cpus {requested} exceeds the configured maximum of {ceiling:g}")
    return value


def raise_unless_ok(result: ExecResult, what: str) -> None:
    """Turn a non-successful ``ExecResult`` into the matching control-flow exception."""
    if result.cancelled:
        raise RunCancelled
    if result.timed_out:
        raise DeadlineExceeded(f"{what} timed out")
    if result.exit_code != 0:
        raise StepFailed(f"{what} exited with code {result.exit_code}")


class ContainerPlanner:
    """Builds the ``RunSpec`` of one ``sh`` step for the agent in force."""

    def __init__(self, ctx: StepContext) -> None:
        self._ctx = ctx
        self._run = ctx.run

    def plan(self, agent: AgentDocker, script: str) -> RunSpec:
        """Resolve image, mounts, network, limits, labels and environment."""
        image = agent.image.plain
        if image is None:
            raise StepFailed("docker image must be a literal string")
        args = parse_agent_args(agent.args.plain if agent.args else None)
        logical = self._run.images.tag_of(image)
        env = {**self._ctx.scope.env, **args.env}
        if logical is not None:
            return self._spec(logical, script, args, env, external=False)
        self._ensure_pulled(image)
        env.setdefault("HOME", DEFAULT_HOME)
        return self._spec(image, script, args, env, external=True)

    def _spec(
        self, image: str, script: str, args: AgentArgs, env: dict[str, str], *, external: bool
    ) -> RunSpec:
        config = self._run.config
        directory = self._ctx.scope.directory
        mounts: tuple[Mount, ...] = ()
        workdir: str | None = None
        user: str | None = None
        if external:
            mounts = (
                Mount(self._run.workspaces.host_path(self._run.job.run_id), WORKSPACE_TARGET),
            )
            workdir = f"{WORKSPACE_TARGET}/{directory}" if directory else WORKSPACE_TARGET
            user = config.container_user
            self._make_directory(directory)
        elif directory:
            script = f"cd -- {shlex.quote(directory)} || exit 1\n{script}"
        step_id = self._ctx.site.step_id
        run_id = self._run.job.run_id
        return RunSpec(
            image=image,
            name=f"sc-r{run_id}-s{step_id}-{uuid.uuid4().hex[:6]}",
            command=("-c", script),
            entrypoint="sh",
            env=env,
            workdir=workdir,
            mounts=mounts,
            network=self._network(args),
            labels={RUN_ID_LABEL: str(run_id), STEP_ID_LABEL: str(step_id)},
            cpus=limited_cpus(args.cpus, config.container_cpus),
            memory=limited_memory(args.memory, config.container_memory),
            user=user,
        )

    def _make_directory(self, directory: str) -> None:
        """Create the ``dir`` on the host so the container user does not create it as root."""
        if not directory:
            return
        workspace = self._run.workspace.resolve()
        target = (workspace / directory).resolve()
        if not target.is_relative_to(workspace):
            raise StepFailed("directory escapes the workspace")
        target.mkdir(parents=True, exist_ok=True)

    def _network(self, args: AgentArgs) -> str | None:
        if args.network_none:
            return "none"
        compose = self._run.job.compose_project
        if not compose:
            return None
        name = f"{compose}_default"
        runtime = self._run.runtime
        if self._run.images.ensure(f"network:{name}", lambda: runtime.network_exists(name)):
            return name
        self._ctx.line(f"network {name} does not exist yet; using the default bridge network")
        return None

    def _ensure_pulled(self, image: str) -> None:
        runtime = self._run.runtime
        if not self._run.images.mark_pulled(image) or runtime.image_exists(image):
            return
        self._ctx.line(f"pulling image {image}")
        result = runtime.pull_image(
            image,
            self._ctx.line,
            timeout=self._ctx.effective_timeout(),
            cancel=self._ctx.cancel_signal,
        )
        raise_unless_ok(result, f"pull of {image}")
