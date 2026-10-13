"""Dry run: what a pipeline would do for a branch, worked out without executing anything.

It reuses the executor's own decisions (``WhenEvaluator`` for ``when``, the step classes for
labels, the scope builder for variables) so the preview cannot drift from a real run. No
container is started, no secret is read and nothing is written.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from shiftcodes.execution.conditions import WhenEvaluator
from shiftcodes.execution.errors import StepFailed
from shiftcodes.execution.plan import SEPARATOR
from shiftcodes.execution.scope import Scope, describe
from shiftcodes.execution.scope_builder import default_params
from shiftcodes.execution.step_registry import STEP_CLASSES
from shiftcodes.pipeline.ast import (
    Agent,
    AgentAny,
    AgentDocker,
    Credentials,
    EnvVar,
    ParallelBody,
    Pipeline,
    Post,
    Stage,
    StepCall,
    StepsBody,
)

PLACEHOLDER_COMMIT = "0" * 40
PLACEHOLDER_NUMBER = "1"


class StageMode(StrEnum):
    """How the contents of a stage are organised."""

    STEPS = "steps"
    PARALLEL = "parallel"
    SEQUENTIAL = "stages"


@dataclass(frozen=True, slots=True)
class DryStep:
    """One step as it would appear in the run; block steps list what is inside them."""

    name: str
    label: str
    children: tuple[DryStep, ...]


@dataclass(frozen=True, slots=True)
class DryPostBlock:
    """A post condition and the steps it would run."""

    condition: str
    steps: tuple[DryStep, ...]


@dataclass(frozen=True, slots=True)
class DryStage:
    """One stage with its verdict for the branch."""

    name: str
    path: str
    will_run: bool
    skip_reason: str | None
    agent: str | None
    mode: StageMode
    steps: tuple[DryStep, ...]
    stages: tuple[DryStage, ...]
    post: tuple[DryPostBlock, ...]


@dataclass(frozen=True, slots=True)
class DryRun:
    """The plan of a whole pipeline for one branch and environment."""

    branch: str
    environment: str
    agent: str | None
    stages: tuple[DryStage, ...]
    post: tuple[DryPostBlock, ...]


def plan_dry_run(pipeline: Pipeline, project: str, environment: str, branch: str) -> DryRun:
    """The stages and steps ``pipeline`` would run on ``branch`` of ``environment``."""
    return _Planner(pipeline, project, environment, branch).run()


class _Planner:
    def __init__(self, pipeline: Pipeline, project: str, environment: str, branch: str) -> None:
        self._pipeline = pipeline
        self._branch = branch
        self._environment = environment
        self._when = WhenEvaluator(branch)
        builtin = {
            "BRANCH_NAME": branch,
            "GIT_COMMIT": PLACEHOLDER_COMMIT,
            "BUILD_NUMBER": PLACEHOLDER_NUMBER,
            "ENVIRONMENT": environment,
            "PROJECT": project,
        }
        self._root = Scope(env=builtin, params=default_params(pipeline))

    def run(self) -> DryRun:
        scope = _with_literals(self._root, self._pipeline.environment)
        stages = tuple(
            self._stage(stage, scope, (), parent_runs=True) for stage in self._pipeline.stages
        )
        return DryRun(
            branch=self._branch,
            environment=self._environment,
            agent=_agent_text(self._pipeline.agent),
            stages=stages,
            post=_post(self._pipeline.post),
        )

    def _stage(
        self, stage: Stage, parent: Scope, prefix: tuple[str, ...], *, parent_runs: bool
    ) -> DryStage:
        path = (*prefix, stage.name)
        reason = None if parent_runs else "an enclosing stage does not run"
        if parent_runs and not self._when.matches(stage.when, parent):
            reason = f"its when condition does not match branch '{self._branch}'"
        runs = reason is None
        scope = _with_literals(parent, stage.environment)
        body = stage.body
        steps: tuple[DryStep, ...] = ()
        children: tuple[DryStage, ...] = ()
        mode = StageMode.STEPS
        if isinstance(body, StepsBody):
            steps = tuple(_step(call) for call in body.steps)
        else:
            mode = StageMode.PARALLEL if isinstance(body, ParallelBody) else StageMode.SEQUENTIAL
            children = tuple(
                self._stage(child, scope, path, parent_runs=runs) for child in body.stages
            )
        return DryStage(
            name=stage.name,
            path=SEPARATOR.join(path),
            will_run=runs,
            skip_reason=reason,
            agent=_agent_text(stage.agent),
            mode=mode,
            steps=steps,
            stages=children,
            post=_post(stage.post),
        )


def _with_literals(scope: Scope, variables: tuple[EnvVar, ...]) -> Scope:
    """Add the variables whose value is known without secrets (credentials are left out)."""
    for var in variables:
        if isinstance(var.value, Credentials):
            continue
        try:
            scope = scope.with_env({var.name: scope.render(var.value)})
        except StepFailed:
            continue
    return scope


def _step(call: StepCall) -> DryStep:
    step_class = STEP_CLASSES.get(call.name)
    label = step_class(call).label() if step_class is not None else call.name
    children = tuple(_step(inner) for inner in call.body or ())
    return DryStep(call.name, label, children)


def _post(post: Post | None) -> tuple[DryPostBlock, ...]:
    if post is None:
        return ()
    return tuple(
        DryPostBlock(block.condition.value, tuple(_step(call) for call in block.steps))
        for block in post.blocks
    )


def _agent_text(agent: Agent | None) -> str | None:
    if agent is None:
        return None
    if isinstance(agent, AgentDocker):
        return f"docker {describe(agent.image)}"
    return "any" if isinstance(agent, AgentAny) else "none"
