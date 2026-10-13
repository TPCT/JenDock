"""The step catalogue shown to editors, generated from the registries in ``steps.py``.

Nothing is written twice: names, types, defaults, required flags, choices and numeric bounds are
read from the same definitions the validator uses, so the forms of the visual editor can never
accept something the validator rejects (or the other way round).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from shiftcodes.pipeline.ast import PostCondition
from shiftcodes.pipeline.steps import (
    OPTIONS,
    PARAMETERS,
    STEPS,
    ArgSpec,
    Block,
    ImageRole,
    IntRange,
    OneOf,
    Registry,
    StepDefinition,
    ValueKind,
)


class ArgType(StrEnum):
    """Argument types as the editors name them."""

    STRING = "string"
    INT = "int"
    BOOL = "bool"
    LIST = "list"
    MAP = "map"


_TYPES = {
    ValueKind.STRING: ArgType.STRING,
    ValueKind.INT: ArgType.INT,
    ValueKind.BOOL: ArgType.BOOL,
    ValueKind.LIST: ArgType.LIST,
    ValueKind.MAP: ArgType.MAP,
}

POST_DESCRIPTIONS: dict[PostCondition, str] = {
    PostCondition.ALWAYS: "Every time, whatever the result was.",
    PostCondition.CHANGED: "When the result is different from the previous run.",
    PostCondition.ABORTED: "When the run was cancelled.",
    PostCondition.FAILURE: "When the run failed.",
    PostCondition.SUCCESS: "When the run succeeded.",
    PostCondition.UNSTABLE: "When the run finished with warnings.",
    PostCondition.CLEANUP: "At the very end, after everything else, whatever happened.",
}


@dataclass(frozen=True, slots=True)
class ArgInfo:
    """One argument of a step as shown in a form."""

    name: str
    type: ArgType
    required: bool
    default: str | int | bool | None
    choices: tuple[str, ...] | None
    minimum: int | None
    maximum: int | None
    description: str
    multiline: bool
    role: ImageRole | None


@dataclass(frozen=True, slots=True)
class StepInfo:
    """One step, option or parameter type."""

    name: str
    title: str
    description: str
    takes_block: bool
    needs_container: bool
    args: tuple[ArgInfo, ...]


@dataclass(frozen=True, slots=True)
class PostInfo:
    """One post condition with its explanation."""

    name: PostCondition
    description: str


@dataclass(frozen=True, slots=True)
class Catalog:
    """Everything the editors need to build forms."""

    steps: tuple[StepInfo, ...]
    options: tuple[StepInfo, ...]
    parameters: tuple[StepInfo, ...]
    post_conditions: tuple[PostInfo, ...]


def _arg_info(spec: ArgSpec) -> ArgInfo:
    check = spec.check
    choices = check.options if isinstance(check, OneOf) else None
    bounds = check if isinstance(check, IntRange) else None
    return ArgInfo(
        name=spec.name,
        type=_TYPES[spec.kind],
        required=spec.required,
        default=spec.default,
        choices=choices,
        minimum=bounds.low if bounds else None,
        maximum=bounds.high if bounds else None,
        description=spec.description,
        multiline=spec.multiline,
        role=spec.role,
    )


def _step_info(definition: StepDefinition) -> StepInfo:
    return StepInfo(
        name=definition.name,
        title=definition.title,
        description=definition.description,
        takes_block=definition.block is Block.REQUIRED,
        needs_container=definition.needs_container,
        args=tuple(_arg_info(spec) for spec in definition.args if spec.rejected is None),
    )


def _infos(registry: Registry) -> tuple[StepInfo, ...]:
    return tuple(_step_info(definition) for definition in registry.values())


def build_catalog() -> Catalog:
    """The catalogue of the current registries (steps keep their registry order)."""
    return Catalog(
        steps=_infos(STEPS),
        options=_infos(OPTIONS),
        parameters=_infos(PARAMETERS),
        post_conditions=tuple(PostInfo(item, POST_DESCRIPTIONS[item]) for item in PostCondition),
    )
