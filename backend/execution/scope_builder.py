"""Initial scopes: built-in variables, parameter defaults, ``environment`` and ``agent``."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from backend.domain.errors import DomainError
from backend.execution.context import RunContext, RunJob
from backend.execution.errors import StepFailed
from backend.execution.scope import Scope
from backend.pipeline.ast import (
    Agent,
    AgentDocker,
    BoolValue,
    Credentials,
    EnvVar,
    ListValue,
    Pipeline,
    StepCall,
    StringValue,
)
from backend.pipeline.steps import arg_value


def builtin_env(job: RunJob) -> dict[str, str]:
    """The variables Jenkins-style pipelines can always use."""
    return {
        "BRANCH_NAME": job.branch,
        "GIT_COMMIT": job.commit_sha,
        "BUILD_NUMBER": str(job.number),
        "ENVIRONMENT": job.environment,
        "PROJECT": job.project,
    }


def default_params(pipeline: Pipeline) -> dict[str, str]:
    """Parameter defaults (runs are not given parameter values yet)."""
    values: dict[str, str] = {}
    for call in pipeline.parameters:
        name = arg_value(call, "name")
        if not isinstance(name, StringValue) or name.plain is None:
            continue
        values[name.plain] = _default(call.name, call)
    return values


def _default(kind: str, call: StepCall) -> str:
    default = arg_value(call, "defaultValue")
    if kind == "booleanParam":
        return "true" if isinstance(default, BoolValue) and default.value else "false"
    if kind == "choice":
        choices = arg_value(call, "choices")
        first = choices.items[0] if isinstance(choices, ListValue) and choices.items else None
        return first.plain or "" if isinstance(first, StringValue) else ""
    return default.plain or "" if isinstance(default, StringValue) else ""


def apply_agent(scope: Scope, agent: Agent | None) -> Scope:
    """A stage or pipeline ``agent`` replaces the one in force (``any``/``none`` mean no docker)."""
    if agent is None:
        return scope
    return replace(scope, agent=agent if isinstance(agent, AgentDocker) else None)


def apply_environment(run: RunContext, variables: Sequence[EnvVar], scope: Scope) -> Scope:
    """Evaluate an ``environment`` section in order; credentials go through the worker."""
    if not variables:
        return scope
    wanted = [var.value.name for var in variables if isinstance(var.value, Credentials)]
    secrets = _resolve(run, wanted)
    for var in variables:
        if isinstance(var.value, Credentials):
            scope = scope.with_env({var.name: secrets[var.value.name]}, frozenset({var.name}))
        else:
            scope = scope.with_env({var.name: scope.render(var.value)})
    return scope


def _resolve(run: RunContext, names: list[str]) -> dict[str, str]:
    if not names:
        return {}
    try:
        values = dict(run.resolve_secrets(names))
    except DomainError as error:
        raise StepFailed(f"cannot resolve credentials: {error}") from error
    for value in values.values():
        run.redactor.register(value)
    return values
