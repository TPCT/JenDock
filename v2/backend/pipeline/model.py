"""The editing model: the JSON form of the pipeline AST that the visual editor reads and writes.

Names mirror ``ast.py`` with spans dropped (see docs/api-contract.md, "The model"). These classes
validate the shape of a request body; whether the content makes sense is the validator's job, so
a half-filled model is a valid ``Model``.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, SerializerFunctionWrapHandler, model_serializer

from backend.pipeline.ast import PostCondition

UNNAMED_KEY = re.compile(r"\$\d+")


def unnamed_key(index: int) -> str:
    """The ``args`` key of the ``index``-th argument that has no name (``$0``, ``$1``, ...)."""
    return f"${index}"


class _Node(BaseModel):
    """Base of every model class: no unknown keys, values are never coerced silently."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class TemplateText(_Node):
    """A double-quoted string: ``${NAME}``, ``${env.NAME}`` and ``${params.NAME}`` are variables.

    A literal ``$`` is written ``\\$`` and a literal backslash ``\\\\``; every other backslash
    stands for itself.
    """

    template: str


class MapItem(_Node):
    """One entry of an ordered map."""

    key: str
    value: Value


class MapText(_Node):
    """An ordered map; the empty map is ``{"map": []}``."""

    map: list[MapItem]


type Str = str | TemplateText
type Value = bool | int | str | TemplateText | MapText | list[Value]


class CredentialsRef(_Node):
    """``credentials('NAME')`` bound to an environment variable."""

    credentials: str


class EnvVar(_Node):
    """One entry of an ``environment`` section."""

    name: str
    value: str | TemplateText | CredentialsRef


class Call(_Node):
    """A step, option or parameter. Positional arguments appear under their catalogue names;
    arguments of unknown steps that have no name are keyed ``$0``, ``$1``, ..."""

    name: str
    args: dict[str, Value] = Field(default_factory=dict)
    body: list[Call] | None = None

    @model_serializer(mode="wrap")
    def _omit_missing_body(self, handler: SerializerFunctionWrapHandler) -> dict[str, object]:
        """``body`` exists only for steps that take a block."""
        data: dict[str, object] = handler(self)
        if data.get("body") is None:
            data.pop("body", None)
        return data


class AgentAny(_Node):
    """``agent any``."""

    type: Literal["any"] = "any"


class AgentNone(_Node):
    """``agent none``."""

    type: Literal["none"] = "none"


class AgentDocker(_Node):
    """``agent { docker { image ...; args ... } }``."""

    type: Literal["docker"] = "docker"
    image: Str
    args: Str | None = None


type Agent = Annotated[AgentAny | AgentNone | AgentDocker, Field(discriminator="type")]


class BranchCondition(_Node):
    """``branch 'pattern'``."""

    type: Literal["branch"] = "branch"
    pattern: Str


class EnvironmentCondition(_Node):
    """``environment name: 'X', value: 'y'``."""

    type: Literal["environment"] = "environment"
    name: str
    value: Str


class NotCondition(_Node):
    """``not { condition }``."""

    type: Literal["not"] = "not"
    condition: Condition


class AllOfCondition(_Node):
    """``allOf { ... }``."""

    type: Literal["allOf"] = "allOf"
    conditions: list[Condition]


class AnyOfCondition(_Node):
    """``anyOf { ... }``."""

    type: Literal["anyOf"] = "anyOf"
    conditions: list[Condition]


type Condition = Annotated[
    BranchCondition | EnvironmentCondition | NotCondition | AllOfCondition | AnyOfCondition,
    Field(discriminator="type"),
]


class PostBlock(_Node):
    """One post condition with its steps."""

    condition: PostCondition
    steps: list[Call]


class Post(_Node):
    """``post { ... }``."""

    blocks: list[PostBlock]


class StepsBody(_Node):
    """``steps { ... }``."""

    type: Literal["steps"] = "steps"
    steps: list[Call]


class ParallelBody(_Node):
    """``parallel { stage... }``."""

    type: Literal["parallel"] = "parallel"
    stages: list[Stage]


class StagesBody(_Node):
    """``stages { stage... }`` nested in a stage."""

    type: Literal["stages"] = "stages"
    stages: list[Stage]


type StageBody = Annotated[StepsBody | ParallelBody | StagesBody, Field(discriminator="type")]


class Stage(_Node):
    """``stage('name') { ... }``."""

    name: str
    agent: Agent | None = None
    environment: list[EnvVar] = Field(default_factory=list)
    options: list[Call] = Field(default_factory=list)
    when: list[Condition] | None = None
    body: StageBody
    post: Post | None = None


class Model(_Node):
    """The whole pipeline."""

    agent: Agent | None = None
    environment: list[EnvVar] = Field(default_factory=list)
    options: list[Call] = Field(default_factory=list)
    parameters: list[Call] = Field(default_factory=list)
    stages: list[Stage] = Field(default_factory=list)
    post: Post | None = None


for _cls in (
    MapItem,
    MapText,
    Call,
    AgentDocker,
    NotCondition,
    AllOfCondition,
    AnyOfCondition,
    ParallelBody,
    StagesBody,
    Stage,
    Model,
):
    _cls.model_rebuild()
