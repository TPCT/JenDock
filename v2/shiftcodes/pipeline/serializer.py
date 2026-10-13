"""Canonical Jenkinsfile text from an AST.

Formatting is fixed: 4-space indent, single quotes unless a string has variables, sections in
Jenkins' conventional order. Comments are not part of the AST and are therefore not preserved:
the visual editor rewrites the canonical form, text-mode edits keep the user's own text.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from contextlib import contextmanager

from shiftcodes.pipeline.ast import (
    Agent,
    AgentAny,
    AgentDocker,
    AgentNone,
    AllOfCondition,
    BoolValue,
    BranchCondition,
    Condition,
    Credentials,
    EnvironmentCondition,
    EnvVar,
    IntValue,
    ListValue,
    NotCondition,
    ParallelBody,
    Pipeline,
    Post,
    PostCondition,
    SequentialBody,
    Stage,
    StepCall,
    StepsBody,
    StringPart,
    StringValue,
    TextPart,
    Value,
    VariablePart,
    VarSource,
    When,
)
from shiftcodes.pipeline.steps import OPTIONS, PARAMETERS, STEPS, Registry, StepDefinition

_INDENT = "    "
_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_RESERVED_KEYS = {"true", "false", "null"}
_POST_ORDER = list(PostCondition)


def serialize(pipeline: Pipeline) -> str:
    """Canonical Jenkinsfile text for ``pipeline`` (ends with a newline)."""
    lines: list[str] = []
    _Writer(lines).pipeline(pipeline)
    return "\n".join(lines) + "\n"


class _Writer:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self._level = 0

    def _line(self, text: str) -> None:
        self._lines.append(f"{_INDENT * self._level}{text}")

    @contextmanager
    def _block(self, head: str) -> Iterator[None]:
        self._line(f"{head} {{")
        self._level += 1
        try:
            yield
        finally:
            self._level -= 1
            self._line("}")

    def pipeline(self, pipeline: Pipeline) -> None:
        with self._block("pipeline"):
            self._agent(pipeline.agent)
            self._environment(pipeline.environment)
            self._calls("options", pipeline.options, OPTIONS)
            self._calls("parameters", pipeline.parameters, PARAMETERS)
            with self._block("stages"):
                self._stages(pipeline.stages)
            self._post(pipeline.post)

    def _stages(self, stages: tuple[Stage, ...]) -> None:
        for stage in stages:
            self._stage(stage)

    def _stage(self, stage: Stage) -> None:
        with self._block(f"stage({_string(stage.name)})"):
            self._agent(stage.agent)
            self._environment(stage.environment)
            self._calls("options", stage.options, OPTIONS)
            self._when(stage.when)
            self._body(stage)
            self._post(stage.post)

    def _body(self, stage: Stage) -> None:
        body = stage.body
        if isinstance(body, StepsBody):
            with self._block("steps"):
                self._steps(body.steps)
        elif isinstance(body, ParallelBody):
            with self._block("parallel"):
                self._stages(body.stages)
        elif isinstance(body, SequentialBody):
            with self._block("stages"):
                self._stages(body.stages)

    # --- sections -----------------------------------------------------------------

    def _agent(self, agent: Agent | None) -> None:
        if agent is None:
            return
        if isinstance(agent, AgentAny | AgentNone):
            self._line("agent any" if isinstance(agent, AgentAny) else "agent none")
            return
        self._docker(agent)

    def _docker(self, agent: AgentDocker) -> None:
        with self._block("agent"), self._block("docker"):
            self._line(f"image {_string_value(agent.image)}")
            if agent.args is not None:
                self._line(f"args {_string_value(agent.args)}")

    def _environment(self, variables: tuple[EnvVar, ...]) -> None:
        if not variables:
            return
        with self._block("environment"):
            for var in variables:
                self._line(f"{var.name} = {_env_value(var)}")

    def _calls(self, section: str, calls: tuple[StepCall, ...], registry: Registry) -> None:
        if not calls:
            return
        with self._block(section):
            self._steps(calls, registry)

    def _when(self, when: When | None) -> None:
        if when is None:
            return
        with self._block("when"):
            self._conditions(when.conditions)

    def _conditions(self, conditions: tuple[Condition, ...]) -> None:
        for condition in conditions:
            self._condition(condition)

    def _condition(self, condition: Condition) -> None:
        if isinstance(condition, BranchCondition):
            self._line(f"branch {_string_value(condition.pattern)}")
        elif isinstance(condition, EnvironmentCondition):
            value = _string_value(condition.value)
            self._line(f"environment name: {_string(condition.name)}, value: {value}")
        elif isinstance(condition, NotCondition):
            with self._block("not"):
                self._condition(condition.condition)
        else:
            keyword = "allOf" if isinstance(condition, AllOfCondition) else "anyOf"
            with self._block(keyword):
                self._conditions(condition.conditions)

    def _post(self, post: Post | None) -> None:
        if post is None:
            return
        with self._block("post"):
            for condition in _POST_ORDER:
                for block in (b for b in post.blocks if b.condition is condition):
                    with self._block(condition.value):
                        self._steps(block.steps)

    # --- steps --------------------------------------------------------------------

    def _steps(self, steps: tuple[StepCall, ...], registry: Registry = STEPS) -> None:
        for call in steps:
            self._call(call, registry)

    def _call(self, call: StepCall, registry: Registry) -> None:
        head = _call_head(call, registry)
        if call.body is None:
            self._line(head)
            return
        with self._block(head):
            self._steps(call.body, registry)


def _call_head(call: StepCall, registry: Registry) -> str:
    definition = registry.get(call.name)
    if definition is not None and _uses_shorthand(definition, call):
        value = _value(call.args[0].value)
        command = definition.command_syntax and call.body is None
        return f"{call.name} {value}" if command else f"{call.name}({value})"
    rendered = ", ".join(_argument(arg.name, arg.value) for arg in call.args)
    return f"{call.name}({rendered})"


def _uses_shorthand(definition: StepDefinition, call: StepCall) -> bool:
    """Whether the single positional-capable argument is written without its name."""
    if definition.named_canonical or not definition.positional or len(call.args) != 1:
        return False
    return call.args[0].name == definition.positional[0]


def _argument(name: str | None, value: Value) -> str:
    return _value(value) if name is None else f"{name}: {_value(value)}"


def _env_value(var: EnvVar) -> str:
    if isinstance(var.value, Credentials):
        return f"credentials({_string(var.value.name)})"
    return _string_value(var.value)


def _value(value: Value) -> str:
    if isinstance(value, StringValue):
        return _string_value(value)
    if isinstance(value, IntValue):
        return str(value.value)
    if isinstance(value, BoolValue):
        return "true" if value.value else "false"
    if isinstance(value, ListValue):
        return "[" + ", ".join(_value(item) for item in value.items) + "]"
    if not value.entries:
        return "[:]"
    entries = (f"{_key(entry.key)}: {_value(entry.value)}" for entry in value.entries)
    return "[" + ", ".join(entries) + "]"


def _key(key: str) -> str:
    return key if _KEY.fullmatch(key) and key not in _RESERVED_KEYS else _string(key)


def _string(text: str) -> str:
    return _render((TextPart(text),))


def _string_value(value: StringValue) -> str:
    return _render(value.parts)


def _render(parts: Sequence[StringPart]) -> str:
    multiline = any("\n" in p.text for p in parts if isinstance(p, TextPart))
    quote = '"' if any(isinstance(p, VariablePart) for p in parts) else "'"
    delimiter = quote * 3 if multiline else quote
    body = "".join(_part(part, quote, multiline) for part in parts)
    return f"{delimiter}{body}{delimiter}"


def _part(part: StringPart, quote: str, multiline: bool) -> str:
    if isinstance(part, TextPart):
        return _escape(part.text, quote, multiline)
    plain = part.source in {VarSource.BARE, VarSource.EXPRESSION}
    prefix = "" if plain else f"{part.source.value}."
    return f"${{{prefix}{part.name}}}"


def _escape(text: str, quote: str, multiline: bool) -> str:
    escaped = text.replace("\\", "\\\\").replace(quote, "\\" + quote).replace("\r", "\\r")
    if quote == '"':
        escaped = escaped.replace("$", "\\$")
    if not multiline:
        escaped = escaped.replace("\n", "\\n").replace("\t", "\\t")
    return escaped
