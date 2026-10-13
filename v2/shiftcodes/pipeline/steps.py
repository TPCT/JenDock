"""Step, option and parameter catalogue: typed signatures and argument validation.

Each ``StepDefinition`` is data; adding a step means adding one definition to a registry.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum

from shiftcodes.pipeline.ast import (
    Argument,
    BoolValue,
    IntValue,
    ListValue,
    MapValue,
    StepCall,
    StringValue,
    TextPart,
    Value,
)
from shiftcodes.pipeline.errors import Span

_LOGICAL_NAME = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}")
_SERVICE_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")
_TARGET = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")
_TIME_UNITS = ("SECONDS", "MINUTES", "HOURS")
_URL_SCHEMES = ("http://", "https://")


class ValueKind(StrEnum):
    """Type of a call argument."""

    STRING = "a string"
    INT = "an integer"
    BOOL = "a boolean"
    LIST = "a list"
    MAP = "a map"


_KIND_TYPES: dict[ValueKind, type] = {
    ValueKind.STRING: StringValue,
    ValueKind.INT: IntValue,
    ValueKind.BOOL: BoolValue,
    ValueKind.LIST: ListValue,
    ValueKind.MAP: MapValue,
}


class Block(StrEnum):
    """Whether a call takes a ``{ ... }`` block of steps."""

    NONE = "none"
    REQUIRED = "required"


@dataclass(frozen=True, slots=True)
class Problem:
    """A validation finding inside one call."""

    message: str
    span: Span


@dataclass(frozen=True, slots=True)
class ImageUse:
    """A logical image a call refers to."""

    name: str
    span: Span


Checker = Callable[[Value], str | None]
CallCheck = Callable[[StepCall], list[Problem]]


@dataclass(frozen=True, slots=True)
class ArgSpec:
    """One allowed argument. ``rejected`` makes the argument a deliberate error."""

    name: str
    kind: ValueKind
    required: bool = False
    default: str | int | bool | None = None
    check: Checker | None = None
    rejected: str | None = None


def _no_problems(_call: StepCall) -> list[Problem]:
    return []


def _no_images(_call: StepCall) -> tuple[str, ...]:
    return ()


def _no_uses(_call: StepCall) -> tuple[ImageUse, ...]:
    return ()


def _no_names(_call: StepCall) -> tuple[str, ...]:
    return ()


@dataclass(frozen=True, slots=True)
class StepDefinition:
    """Signature and semantics of a step, option or parameter."""

    name: str
    args: tuple[ArgSpec, ...] = ()
    positional: tuple[str, ...] = ()
    block: Block = Block.NONE
    command_syntax: bool = False
    named_canonical: bool = False
    needs_container: bool = False
    cross_check: CallCheck = field(default=_no_problems)
    produced_images: Callable[[StepCall], tuple[str, ...]] = field(default=_no_images)
    image_uses: Callable[[StepCall], tuple[ImageUse, ...]] = field(default=_no_uses)
    bound_env: Callable[[StepCall], tuple[str, ...]] = field(default=_no_names)

    def spec(self, name: str) -> ArgSpec | None:
        """The argument spec called ``name``."""
        return next((arg for arg in self.args if arg.name == name), None)


Registry = Mapping[str, StepDefinition]


def arg_value(call: StepCall, name: str) -> Value | None:
    """The value of the named argument of ``call``."""
    return next((a.value for a in call.args if a.name == name), None)


def _plain(value: Value) -> str | None:
    return value.plain if isinstance(value, StringValue) else None


def _leading_text(value: Value) -> str:
    """Literal text a string starts with ('' when it starts with a variable)."""
    if isinstance(value, StringValue) and value.parts and isinstance(value.parts[0], TextPart):
        return value.parts[0].text
    return ""


def _literal_problem(what: str) -> str:
    return f"{what} must be a literal string without variables"


def positive_int(value: Value) -> str | None:
    """Integer greater than zero."""
    ok = isinstance(value, IntValue) and value.value > 0
    return None if ok else "must be greater than 0"


def int_range(low: int, high: int) -> Checker:
    """Integer between ``low`` and ``high`` inclusive."""

    def check(value: Value) -> str | None:
        ok = isinstance(value, IntValue) and low <= value.value <= high
        return None if ok else f"must be between {low} and {high}"

    return check


def one_of(options: tuple[str, ...]) -> Checker:
    """Literal string equal to one of ``options``."""

    def check(value: Value) -> str | None:
        ok = _plain(value) in options
        return None if ok else f"must be one of {', '.join(options)}"

    return check


def pattern(regex: re.Pattern[str], what: str) -> Checker:
    """Literal string matching ``regex``."""

    def check(value: Value) -> str | None:
        text = _plain(value)
        if text is None:
            return _literal_problem(what)
        return None if regex.fullmatch(text) else f"{what} {text!r} is not valid"

    return check


def relative_path(value: Value) -> str | None:
    """Literal relative path without ``..`` segments or NUL."""
    text = _plain(value)
    if text is None:
        return _literal_problem("path")
    bad = not text or text.startswith("/") or "\0" in text or ".." in text.split("/")
    return "path must be relative and must not contain '..'" if bad else None


def http_url(value: Value) -> str | None:
    """String starting with ``http://`` or ``https://`` (variables allowed after that)."""
    ok = _leading_text(value).startswith(_URL_SCHEMES)
    return None if ok else "must start with http:// or https://"


def env_assignments(value: Value) -> str | None:
    """List of ``NAME=value`` strings."""
    if not isinstance(value, ListValue):
        return "must be a list"
    for item in value.items:
        name = _assignment_name(item)
        if name is None or not _ENV_NAME.fullmatch(name):
            return "every entry must be a string 'NAME=value' with NAME like [A-Z_][A-Z0-9_]*"
    return None


def _assignment_name(item: Value) -> str | None:
    name, equals, _ = _leading_text(item).partition("=")
    return name if equals else None


def string_map(key: re.Pattern[str], value_check: Checker | None = None) -> Checker:
    """Map from keys matching ``key`` to string values accepted by ``value_check``."""

    def check(value: Value) -> str | None:
        if not isinstance(value, MapValue):
            return "must be a map"
        for entry in value.entries:
            valid = key.fullmatch(entry.key) and isinstance(entry.value, StringValue)
            if not valid or (value_check and value_check(entry.value)):
                return f"entry {entry.key!r} must be a valid name mapped to a valid string"
        return None

    return check


def choice_list(value: Value) -> str | None:
    """Non-empty list of literal strings."""
    if not isinstance(value, ListValue) or not value.items:
        return "must be a non-empty list"
    if any(_plain(item) is None for item in value.items):
        return "choices must be literal strings"
    return None


def check_call(definition: StepDefinition, call: StepCall) -> list[Problem]:
    """Validate the arguments and block of ``call`` against ``definition``."""
    problems = _check_block(definition, call)
    seen: set[str] = set()
    for arg in call.args:
        problems.extend(_check_argument(definition, arg, seen))
    problems.extend(_missing_required(definition, call, seen))
    if not any(p.message.startswith("unknown argument") for p in problems):
        problems.extend(definition.cross_check(call))
    return problems


def _check_block(definition: StepDefinition, call: StepCall) -> list[Problem]:
    if definition.block is Block.REQUIRED and call.body is None:
        return [Problem(f"'{call.name}' needs a block: {call.name}(...) {{ ... }}", call.span)]
    if definition.block is Block.NONE and call.body is not None:
        return [Problem(f"'{call.name}' does not take a block", call.span)]
    return []


def _check_argument(definition: StepDefinition, arg: Argument, seen: set[str]) -> list[Problem]:
    if arg.name is None:
        return [Problem(f"unexpected positional argument for '{definition.name}'", arg.span)]
    spec = definition.spec(arg.name)
    if spec is None:
        allowed = ", ".join(a.name for a in definition.args) or "none"
        message = f"unknown argument '{arg.name}' for '{definition.name}'; allowed: {allowed}"
        return [Problem(message, arg.span)]
    if arg.name in seen:
        return [Problem(f"duplicate argument '{arg.name}'", arg.span)]
    seen.add(arg.name)
    return _check_value(definition, spec, arg)


def _check_value(definition: StepDefinition, spec: ArgSpec, arg: Argument) -> list[Problem]:
    if spec.rejected:
        return [Problem(f"'{spec.name}' is not supported: {spec.rejected}", arg.span)]
    if not isinstance(arg.value, _KIND_TYPES[spec.kind]):
        message = f"argument '{spec.name}' of '{definition.name}' must be {spec.kind.value}"
        return [Problem(message, arg.value.span)]
    problem = spec.check(arg.value) if spec.check else None
    if problem:
        message = f"argument '{spec.name}' of '{definition.name}': {problem}"
        return [Problem(message, arg.value.span)]
    return []


def _missing_required(definition: StepDefinition, call: StepCall, seen: set[str]) -> list[Problem]:
    return [
        Problem(f"'{call.name}' requires argument '{spec.name}'", call.span)
        for spec in definition.args
        if spec.required and spec.name not in seen
    ]


def _deploy_cross_check(call: StepCall) -> list[Problem]:
    names = {a.name for a in call.args}
    single = {"service", "image"} & names
    if "services" in names and single:
        return [Problem("use either service/image or services, not both", call.span)]
    if "services" in names:
        return []
    if single != {"service", "image"}:
        message = "deploy needs service and image, or services: [service: image, ...]"
        return [Problem(message, call.span)]
    return []


def _deploy_uses(call: StepCall) -> tuple[ImageUse, ...]:
    image = arg_value(call, "image")
    services = arg_value(call, "services")
    values: list[Value] = [image] if image is not None else []
    if isinstance(services, MapValue):
        values.extend(entry.value for entry in services.entries)
    return tuple(
        ImageUse(text, value.span) for value in values if (text := _plain(value)) is not None
    )


def _build_produces(call: StepCall) -> tuple[str, ...]:
    image = arg_value(call, "image")
    text = _plain(image) if image is not None else None
    return (text,) if text is not None else ()


def _with_env_names(call: StepCall) -> tuple[str, ...]:
    overrides = arg_value(call, "overrides")
    if not isinstance(overrides, ListValue):
        return ()
    names = (_assignment_name(item) for item in overrides.items)
    return tuple(name for name in names if name is not None)


def _string(
    name: str,
    *,
    required: bool = False,
    default: str | None = None,
    check: Checker | None = None,
) -> ArgSpec:
    return ArgSpec(name, ValueKind.STRING, required=required, default=default, check=check)


_NO_GROOVY = "there is no Groovy to consume the result"
_TIMEOUT_ARGS = (
    ArgSpec("time", ValueKind.INT, required=True, check=positive_int),
    ArgSpec("unit", ValueKind.STRING, check=one_of(_TIME_UNITS), default="MINUTES"),
)
_RETRY_ARGS = (ArgSpec("count", ValueKind.INT, required=True, check=int_range(1, 10)),)
_LOGICAL = pattern(_LOGICAL_NAME, "image name")
_SERVICES = string_map(_SERVICE_NAME, _LOGICAL)
_SH = StepDefinition(
    "sh",
    (
        _string("script", required=True),
        _string("label"),
        ArgSpec("returnStatus", ValueKind.BOOL, rejected=_NO_GROOVY),
        ArgSpec("returnStdout", ValueKind.BOOL, rejected=_NO_GROOVY),
    ),
    positional=("script",),
    command_syntax=True,
    needs_container=True,
)
_TIMEOUT = StepDefinition("timeout", _TIMEOUT_ARGS, ("time",), Block.REQUIRED, named_canonical=True)
_RETRY = StepDefinition("retry", _RETRY_ARGS, ("count",), Block.REQUIRED)

STEPS: Registry = {
    definition.name: definition
    for definition in (
        _SH,
        StepDefinition(
            "echo", (_string("message", required=True),), ("message",), command_syntax=True
        ),
        StepDefinition(
            "error", (_string("message", required=True),), ("message",), command_syntax=True
        ),
        _TIMEOUT,
        _RETRY,
        StepDefinition(
            "dir",
            (_string("path", required=True, check=relative_path),),
            ("path",),
            Block.REQUIRED,
        ),
        StepDefinition(
            "withEnv",
            (ArgSpec("overrides", ValueKind.LIST, required=True, check=env_assignments),),
            ("overrides",),
            Block.REQUIRED,
            bound_env=_with_env_names,
        ),
        StepDefinition(
            "dockerBuild",
            (
                _string("image", required=True, check=_LOGICAL),
                _string("dockerfile", default="Dockerfile", check=relative_path),
                _string("context", default=".", check=relative_path),
                _string("target", check=pattern(_TARGET, "target")),
                ArgSpec("args", ValueKind.MAP, check=string_map(_IDENTIFIER)),
            ),
            produced_images=_build_produces,
        ),
        StepDefinition(
            "deploy",
            (
                _string("service", check=pattern(_SERVICE_NAME, "service name")),
                _string("image", check=_LOGICAL),
                ArgSpec("services", ValueKind.MAP, check=_SERVICES),
            ),
            cross_check=_deploy_cross_check,
            image_uses=_deploy_uses,
        ),
        StepDefinition(
            "healthCheck",
            (
                _string("url", required=True, check=http_url),
                ArgSpec("expectStatus", ValueKind.INT, default=200, check=int_range(100, 599)),
                ArgSpec("timeout", ValueKind.INT, default=60, check=int_range(1, 3600)),
            ),
        ),
    )
}

OPTIONS: Registry = {
    definition.name: definition
    for definition in (
        replace(_TIMEOUT, block=Block.NONE),
        replace(_RETRY, block=Block.NONE),
        StepDefinition("disableConcurrentBuilds"),
        StepDefinition("skipDefaultCheckout"),
    )
}

_PARAM_NAME = _string("name", required=True, check=pattern(_IDENTIFIER, "parameter name"))
_DESCRIPTION = _string("description")
PARAMETERS: Registry = {
    definition.name: definition
    for definition in (
        StepDefinition("string", (_PARAM_NAME, _string("defaultValue"), _DESCRIPTION)),
        StepDefinition(
            "booleanParam",
            (_PARAM_NAME, ArgSpec("defaultValue", ValueKind.BOOL), _DESCRIPTION),
        ),
        StepDefinition(
            "choice",
            (
                _PARAM_NAME,
                ArgSpec("choices", ValueKind.LIST, required=True, check=choice_list),
                _DESCRIPTION,
            ),
        ),
    )
}


def unknown_message(kind: str, name: str, registry: Registry) -> str:
    """Error text for a ``kind`` ('step', 'option', 'parameter type') missing in ``registry``."""
    return f"unknown {kind} '{name}'; supported: {', '.join(sorted(registry))}"
