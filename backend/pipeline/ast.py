"""Typed AST of a Declarative Pipeline subset.

Every node is a frozen dataclass with a ``span``. Spans are excluded from equality so that
two parses of equivalent text (or a parse of serialized output) compare equal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from backend.pipeline.errors import Span


class VarSource(StrEnum):
    """How an interpolated variable was written."""

    BARE = "bare"  # $NAME or ${NAME}
    ENV = "env"  # ${env.NAME}
    PARAMS = "params"  # ${params.NAME}
    EXPRESSION = "expression"  # anything else; ``name`` holds the raw text and is invalid


@dataclass(frozen=True, slots=True)
class TextPart:
    """Literal text of a string (escapes already decoded)."""

    text: str


@dataclass(frozen=True, slots=True)
class VariablePart:
    """A variable reference inside a double-quoted string; never evaluated by the engine."""

    source: VarSource
    name: str
    span: Span = field(compare=False)


StringPart = TextPart | VariablePart


@dataclass(frozen=True, slots=True)
class StringValue:
    """A string as literal and variable parts (adjacent text parts are merged)."""

    parts: tuple[StringPart, ...]
    span: Span = field(compare=False)

    @property
    def plain(self) -> str | None:
        """The text when the string has no variables, otherwise ``None``."""
        if all(isinstance(part, TextPart) for part in self.parts):
            return "".join(part.text for part in self.parts if isinstance(part, TextPart))
        return None

    @property
    def variables(self) -> tuple[VariablePart, ...]:
        """The variable parts in order."""
        return tuple(part for part in self.parts if isinstance(part, VariablePart))


@dataclass(frozen=True, slots=True)
class IntValue:
    """Integer literal."""

    value: int
    span: Span = field(compare=False)


@dataclass(frozen=True, slots=True)
class BoolValue:
    """``true`` or ``false``."""

    value: bool
    span: Span = field(compare=False)


@dataclass(frozen=True, slots=True)
class ListValue:
    """``[a, b]``."""

    items: tuple[Value, ...]
    span: Span = field(compare=False)


@dataclass(frozen=True, slots=True)
class MapEntry:
    """One ``key: value`` pair of a map."""

    key: str
    value: Value
    span: Span = field(compare=False)


@dataclass(frozen=True, slots=True)
class MapValue:
    """``[k: v, ...]`` (the empty map is ``[:]``)."""

    entries: tuple[MapEntry, ...]
    span: Span = field(compare=False)


Value = StringValue | IntValue | BoolValue | ListValue | MapValue


@dataclass(frozen=True, slots=True)
class Argument:
    """A call argument. ``name`` is ``None`` only for positional arguments the step
    definition does not map (unknown steps or too many arguments)."""

    name: str | None
    value: Value
    span: Span = field(compare=False)


@dataclass(frozen=True, slots=True)
class StepCall:
    """A step, option or parameter call; ``body`` is set for block steps."""

    name: str
    args: tuple[Argument, ...]
    body: tuple[StepCall, ...] | None
    span: Span = field(compare=False)


@dataclass(frozen=True, slots=True)
class AgentAny:
    """``agent any``."""

    span: Span = field(compare=False)


@dataclass(frozen=True, slots=True)
class AgentNone:
    """``agent none``."""

    span: Span = field(compare=False)


@dataclass(frozen=True, slots=True)
class AgentDocker:
    """``agent { docker { image '...'; args '...' } }``."""

    image: StringValue
    args: StringValue | None
    span: Span = field(compare=False)


Agent = AgentAny | AgentNone | AgentDocker


@dataclass(frozen=True, slots=True)
class Credentials:
    """``credentials('NAME')`` bound to an environment variable."""

    name: str
    span: Span = field(compare=False)


@dataclass(frozen=True, slots=True)
class EnvVar:
    """One ``NAME = value`` entry of an ``environment`` section."""

    name: str
    value: StringValue | Credentials
    span: Span = field(compare=False)


@dataclass(frozen=True, slots=True)
class BranchCondition:
    """``branch 'pattern'``."""

    pattern: StringValue
    span: Span = field(compare=False)


@dataclass(frozen=True, slots=True)
class EnvironmentCondition:
    """``environment name: 'X', value: 'y'``."""

    name: str
    value: StringValue
    span: Span = field(compare=False)


@dataclass(frozen=True, slots=True)
class NotCondition:
    """``not { condition }``."""

    condition: Condition
    span: Span = field(compare=False)


@dataclass(frozen=True, slots=True)
class AllOfCondition:
    """``allOf { ... }``."""

    conditions: tuple[Condition, ...]
    span: Span = field(compare=False)


@dataclass(frozen=True, slots=True)
class AnyOfCondition:
    """``anyOf { ... }``."""

    conditions: tuple[Condition, ...]
    span: Span = field(compare=False)


Condition = BranchCondition | EnvironmentCondition | NotCondition | AllOfCondition | AnyOfCondition


@dataclass(frozen=True, slots=True)
class When:
    """``when { ... }``; several top-level conditions mean all of them."""

    conditions: tuple[Condition, ...]
    span: Span = field(compare=False)


class PostCondition(StrEnum):
    """Post sections in Jenkins' conventional order."""

    ALWAYS = "always"
    CHANGED = "changed"
    ABORTED = "aborted"
    FAILURE = "failure"
    SUCCESS = "success"
    UNSTABLE = "unstable"
    CLEANUP = "cleanup"


@dataclass(frozen=True, slots=True)
class PostBlock:
    """One post condition with its steps."""

    condition: PostCondition
    steps: tuple[StepCall, ...]
    span: Span = field(compare=False)


@dataclass(frozen=True, slots=True)
class Post:
    """``post { ... }``."""

    blocks: tuple[PostBlock, ...]
    span: Span = field(compare=False)


@dataclass(frozen=True, slots=True)
class StepsBody:
    """``steps { ... }``."""

    steps: tuple[StepCall, ...]
    span: Span = field(compare=False)


@dataclass(frozen=True, slots=True)
class ParallelBody:
    """``parallel { stage(...) { ... } ... }``."""

    stages: tuple[Stage, ...]
    span: Span = field(compare=False)


@dataclass(frozen=True, slots=True)
class SequentialBody:
    """``stages { stage(...) { ... } ... }`` nested in a stage."""

    stages: tuple[Stage, ...]
    span: Span = field(compare=False)


StageBody = StepsBody | ParallelBody | SequentialBody


@dataclass(frozen=True, slots=True)
class Stage:
    """``stage('name') { ... }``."""

    name: str
    agent: Agent | None
    environment: tuple[EnvVar, ...]
    options: tuple[StepCall, ...]
    when: When | None
    body: StageBody
    post: Post | None
    span: Span = field(compare=False)


@dataclass(frozen=True, slots=True)
class Pipeline:
    """The single ``pipeline { ... }`` of a Jenkinsfile."""

    agent: Agent | None
    environment: tuple[EnvVar, ...]
    options: tuple[StepCall, ...]
    parameters: tuple[StepCall, ...]
    stages: tuple[Stage, ...]
    post: Post | None
    span: Span = field(compare=False)
