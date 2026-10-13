"""Semantic validation of a parsed pipeline. Collects every finding, never stops early."""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

from backend.pipeline.ast import (
    Agent,
    AgentDocker,
    AllOfCondition,
    AnyOfCondition,
    BranchCondition,
    Condition,
    Credentials,
    EnvironmentCondition,
    EnvVar,
    ListValue,
    MapValue,
    NotCondition,
    ParallelBody,
    Pipeline,
    Post,
    SequentialBody,
    Stage,
    StepCall,
    StepsBody,
    StringValue,
    Value,
    VariablePart,
    VarSource,
    When,
)
from backend.pipeline.docker import check_docker_args, is_external_reference
from backend.pipeline.errors import Diagnostic, Severity, Span
from backend.pipeline.interpolation import VariableScope, variable_problem
from backend.pipeline.steps import (
    OPTIONS,
    PARAMETERS,
    STEPS,
    Problem,
    Registry,
    arg_value,
    check_call,
    unknown_message,
)

_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")
_NO_CONTAINER = "sh runs inside a container: set agent { docker { image '...' } }"


@dataclass(frozen=True, slots=True)
class ValidationContext:
    """Facts about the target project that the text itself cannot tell."""

    secret_names: frozenset[str] = frozenset()
    # Logical images that exist before the pipeline runs (a redeploy reuses built images).
    built_images: frozenset[str] = frozenset()
    # False when the target environment is unknown (editor without a project): credentials()
    # names cannot be judged then and are not reported.
    check_credentials: bool = True


def validate(pipeline: Pipeline, context: ValidationContext) -> list[Diagnostic]:
    """All diagnostics for ``pipeline``, sorted by position."""
    found = _Validator(context).run(pipeline)
    return sorted(found, key=lambda d: (d.span.line, d.span.column))


@dataclass(frozen=True, slots=True)
class _Scope:
    variables: VariableScope
    has_container: bool


class _Validator:
    def __init__(self, context: ValidationContext) -> None:
        self._secrets = context.secret_names
        self._check_credentials = context.check_credentials
        self._found: list[Diagnostic] = []
        self._built: set[str] = set(context.built_images)

    def run(self, pipeline: Pipeline) -> list[Diagnostic]:
        params = self._parameters(pipeline.parameters)
        scope = _Scope(VariableScope(frozenset(), params, frozenset()), False)
        if pipeline.agent is None:
            self._error(
                "missing required 'agent' section (agent any, none or docker)", pipeline.span
            )
        else:
            self._agent(pipeline.agent)
            scope = replace(scope, has_container=isinstance(pipeline.agent, AgentDocker))
        scope = self._environment(pipeline.environment, scope)
        self._calls(pipeline.options, OPTIONS, "option")
        if not pipeline.stages:
            self._error("pipeline needs at least one stage", pipeline.span)
        self._stages(pipeline.stages, scope)
        self._post(pipeline.post, scope)
        return self._found

    # --- reporting ----------------------------------------------------------------

    def _error(self, message: str, span: Span) -> None:
        self._found.append(Diagnostic(Severity.ERROR, message, span))

    def _warning(self, message: str, span: Span) -> None:
        self._found.append(Diagnostic(Severity.WARNING, message, span))

    def _problems(self, problems: list[Problem]) -> None:
        for problem in problems:
            self._error(problem.message, problem.span)

    # --- parameters, options, environment -----------------------------------------

    def _calls(self, calls: tuple[StepCall, ...], registry: Registry, kind: str) -> None:
        seen: set[str] = set()
        for call in calls:
            if kind == "option" and call.name in seen:
                self._error(f"duplicate option '{call.name}'", call.span)
            seen.add(call.name)
            definition = registry.get(call.name)
            if definition is None:
                self._error(unknown_message(kind, call.name, registry), call.span)
            else:
                self._problems(check_call(definition, call))

    def _parameters(self, calls: tuple[StepCall, ...]) -> frozenset[str]:
        self._calls(calls, PARAMETERS, "parameter type")
        names: set[str] = set()
        for call in calls:
            value = arg_value(call, "name")
            name = value.plain if isinstance(value, StringValue) else None
            if name is None:
                continue
            if name in names:
                self._error(f"duplicate parameter '{name}'", call.span)
            names.add(name)
        return frozenset(names)

    def _environment(self, variables: tuple[EnvVar, ...], scope: _Scope) -> _Scope:
        defined: set[str] = set()
        for var in variables:
            self._env_name(var, defined)
            if isinstance(var.value, Credentials):
                self._credential(var.value)
            else:
                self._strings(var.value, scope)
            secret = frozenset({var.name}) if isinstance(var.value, Credentials) else frozenset()
            scope = replace(
                scope, variables=scope.variables.with_env(frozenset({var.name}), secret)
            )
            defined.add(var.name)
        return scope

    def _env_name(self, var: EnvVar, defined: set[str]) -> None:
        if not _ENV_NAME.fullmatch(var.name):
            self._error(f"environment name '{var.name}' must match [A-Z_][A-Z0-9_]*", var.span)
        if var.name in defined:
            self._error(f"duplicate environment variable '{var.name}'", var.span)

    def _credential(self, credential: Credentials) -> None:
        if self._check_credentials and credential.name not in self._secrets:
            message = f"unknown credential '{credential.name}'; create the secret first"
            self._error(message, credential.span)

    # --- agents -------------------------------------------------------------------

    def _agent(self, agent: Agent) -> None:
        if not isinstance(agent, AgentDocker):
            return
        image = agent.image.plain
        if image is None:
            self._error("docker image must be a literal string without variables", agent.image.span)
        elif image not in self._built and not is_external_reference(image):
            self._error(_image_message(image), agent.image.span)
        if agent.args is not None:
            self._docker_args(agent.args)

    def _docker_args(self, args: StringValue) -> None:
        text = args.plain
        if text is None:
            self._error("docker args must be a literal string without variables", args.span)
            return
        problem = check_docker_args(text)
        if problem:
            self._error(problem, args.span)

    # --- stages -------------------------------------------------------------------

    def _stages(self, stages: tuple[Stage, ...], scope: _Scope) -> None:
        self._unique_names(stages)
        for stage in stages:
            self._stage(stage, scope)

    def _unique_names(self, stages: tuple[Stage, ...]) -> None:
        first: dict[str, Span] = {}
        for stage in stages:
            if not stage.name.strip():
                self._error("stage name must not be empty", stage.span)
            elif stage.name in first:
                where = f"line {first[stage.name].line}"
                self._error(
                    f"duplicate stage name '{stage.name}' (first used at {where})", stage.span
                )
            else:
                first[stage.name] = stage.span

    def _stage(self, stage: Stage, scope: _Scope) -> None:
        if stage.agent is not None:
            self._agent(stage.agent)
            scope = replace(scope, has_container=isinstance(stage.agent, AgentDocker))
        scope = self._environment(stage.environment, scope)
        self._calls(stage.options, OPTIONS, "option")
        if stage.when is not None:
            self._when(stage.when)
        self._body(stage, scope)
        self._post(stage.post, scope)

    def _body(self, stage: Stage, scope: _Scope) -> None:
        body = stage.body
        if isinstance(body, StepsBody):
            if not body.steps:
                self._error("steps section is empty", body.span)
            self._steps(body.steps, scope)
        elif isinstance(body, ParallelBody):
            self._parallel(body, scope)
        elif isinstance(body, SequentialBody):
            if not body.stages:
                self._error("stages section is empty", body.span)
            self._stages(body.stages, scope)

    def _parallel(self, body: ParallelBody, scope: _Scope) -> None:
        if not body.stages:
            self._error("parallel needs at least one stage", body.span)
        self._unique_names(body.stages)
        before = set(self._built)
        after = set(before)
        for stage in body.stages:
            self._built = set(before)
            self._stage(stage, scope)
            after |= self._built
        self._built = after

    # --- when ---------------------------------------------------------------------

    def _when(self, when: When) -> None:
        for condition in when.conditions:
            self._condition(condition)

    def _condition(self, condition: Condition) -> None:
        if isinstance(condition, BranchCondition):
            self._literal_condition(condition.pattern, "branch pattern")
        elif isinstance(condition, EnvironmentCondition):
            if not _ENV_NAME.fullmatch(condition.name):
                self._error(f"environment name '{condition.name}' is not valid", condition.span)
            self._literal_condition(condition.value, "environment value")
        elif isinstance(condition, NotCondition):
            self._condition(condition.condition)
        elif isinstance(condition, AllOfCondition | AnyOfCondition):
            for inner in condition.conditions:
                self._condition(inner)

    def _literal_condition(self, value: StringValue, what: str) -> None:
        if value.plain is None:
            self._error(f"{what} must be a literal string without variables", value.span)
        elif not value.plain:
            self._error(f"{what} must not be empty", value.span)

    # --- steps --------------------------------------------------------------------

    def _post(self, post: Post | None, scope: _Scope) -> None:
        for block in post.blocks if post else ():
            if not block.steps:
                self._error(f"post condition '{block.condition.value}' has no steps", block.span)
            self._steps(block.steps, scope)

    def _steps(self, steps: tuple[StepCall, ...], scope: _Scope) -> None:
        for call in steps:
            self._step(call, scope)

    def _step(self, call: StepCall, scope: _Scope) -> None:
        definition = STEPS.get(call.name)
        if definition is None:
            self._error(unknown_message("step", call.name, STEPS), call.span)
            self._steps(call.body or (), scope)
            return
        self._problems(check_call(definition, call))
        if definition.needs_container and not scope.has_container:
            self._error(_NO_CONTAINER, call.span)
        for arg in call.args:
            self._strings(arg.value, scope)
        for use in definition.image_uses(call):
            if use.name not in self._built:
                self._error(_unbuilt_message(use.name), use.span)
        self._built.update(definition.produced_images(call))
        names = frozenset(definition.bound_env(call))
        body_scope = replace(scope, variables=scope.variables.with_env(names))
        self._steps(call.body or (), body_scope)

    # --- interpolation ------------------------------------------------------------

    def _strings(self, value: Value, scope: _Scope) -> None:
        if isinstance(value, StringValue):
            for part in value.variables:
                self._variable(part, scope)
        elif isinstance(value, ListValue):
            for item in value.items:
                self._strings(item, scope)
        elif isinstance(value, MapValue):
            for entry in value.entries:
                self._strings(entry.value, scope)

    def _variable(self, part: VariablePart, scope: _Scope) -> None:
        problem = variable_problem(part, scope.variables)
        if problem:
            self._error(problem, part.span)
        elif part.source is not VarSource.PARAMS and part.name in scope.variables.secrets:
            self._warning(
                f"secret '{part.name}' is interpolated into a double-quoted string; "
                "use single quotes so the shell reads it from the environment",
                part.span,
            )


def _image_message(image: str) -> str:
    return (
        f"docker image '{image}' is not built by an earlier dockerBuild and is not a valid "
        "external reference with a tag or digest (for example node:22)"
    )


def _unbuilt_message(image: str) -> str:
    return f"image '{image}' is not built by an earlier dockerBuild step"
