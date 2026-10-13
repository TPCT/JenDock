"""Convert a v1 recipe into a canonical Jenkinsfile (AST + serializer, never string templates).

Shape: a ``Build`` stage with one ``dockerBuild`` per image (test-only images included), one
``Test`` stage per v1 test running in its image (the test environment becomes stage
``environment``), and a ``Deploy`` stage with ``deploy services: [service: image]`` followed by
the v1 HTTP checks as ``healthCheck`` steps.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass

from shiftcodes.importer.compose_images import ServiceImage
from shiftcodes.importer.v1_model import V1Check, V1Image, V1Recipe, V1Test
from shiftcodes.pipeline import serialize
from shiftcodes.pipeline.ast import (
    AgentDocker,
    AgentNone,
    Argument,
    EnvVar,
    IntValue,
    MapEntry,
    MapValue,
    Pipeline,
    Stage,
    StepCall,
    StepsBody,
    StringValue,
    TextPart,
    Value,
)
from shiftcodes.pipeline.errors import Span

SPAN = Span(1, 1)
ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]*")
SENSITIVE_NAME = re.compile(r"SECRET|PASSWORD|TOKEN|PRIVATE|CREDENTIAL|API_?KEY", re.IGNORECASE)
SHELL_WRAPPER = ("sh", "-c")
MIN_SHELL_WRAPPER_LENGTH = 3


@dataclass(frozen=True, slots=True)
class GeneratedPipeline:
    """The Jenkinsfile text and everything the operator should know about its generation."""

    jenkinsfile: str
    warnings: tuple[str, ...]


def build_pipeline(recipe: V1Recipe, services: list[ServiceImage]) -> GeneratedPipeline:
    """Generate the Jenkinsfile of ``recipe``; ``services`` maps each service to its image."""
    warnings: list[str] = []
    stages = [_build_stage(recipe.images)]
    stages.extend(_test_stages(recipe.tests, warnings))
    deploy = _deploy_stage(recipe, services, warnings)
    if deploy is not None:
        stages.append(deploy)
    pipeline = Pipeline(
        agent=AgentNone(SPAN),
        environment=(),
        options=(),
        parameters=(),
        stages=tuple(stages),
        post=None,
        span=SPAN,
    )
    return GeneratedPipeline(serialize(pipeline), tuple(warnings))


# --- node helpers --------------------------------------------------------------------------


def _text(value: str) -> StringValue:
    return StringValue((TextPart(value),), SPAN)


def _arg(name: str, value: Value) -> Argument:
    return Argument(name, value, SPAN)


def _call(name: str, *args: Argument) -> StepCall:
    return StepCall(name, args, None, SPAN)


def _stage(
    name: str,
    steps: list[StepCall],
    *,
    agent: AgentDocker | None = None,
    environment: tuple[EnvVar, ...] = (),
) -> Stage:
    return Stage(name, agent, environment, (), None, StepsBody(tuple(steps), SPAN), None, SPAN)


# --- stages --------------------------------------------------------------------------------


def _build_stage(images: tuple[V1Image, ...]) -> Stage:
    return _stage("Build", [_docker_build(image) for image in images])


def _docker_build(image: V1Image) -> StepCall:
    args = [_arg("image", _text(image.name)), _arg("dockerfile", _text(image.dockerfile))]
    if image.context:
        args.append(_arg("context", _text(image.context)))
    if image.target:
        args.append(_arg("target", _text(image.target)))
    if image.build_args:
        entries = tuple(MapEntry(k, _text(v), SPAN) for k, v in image.build_args.items())
        args.append(_arg("args", MapValue(entries, SPAN)))
    return _call("dockerBuild", *args)


def _test_stages(tests: tuple[V1Test, ...], warnings: list[str]) -> list[Stage]:
    names = _stage_names(tests)
    return [_test_stage(name, test, warnings) for name, test in zip(names, tests, strict=True)]


def _stage_names(tests: tuple[V1Test, ...]) -> list[str]:
    if len(tests) == 1:
        return ["Test"]
    counts: dict[str, int] = {}
    names = []
    for test in tests:
        counts[test.image] = counts.get(test.image, 0) + 1
        suffix = "" if counts[test.image] == 1 else f" ({counts[test.image]})"
        names.append(f"Test {test.image}{suffix}")
    return names


def _test_stage(name: str, test: V1Test, warnings: list[str]) -> Stage:
    where = f"test in {test.image}"
    if test.tmpfs:
        warnings.append(
            f"{where}: tmpfs mounts {list(test.tmpfs)} are not supported by v2 and were dropped; "
            "make the test create the directories it needs (it may be slower or need writable dirs)"
        )
    environment = tuple(_env_vars(test, where, warnings))
    steps = [_call("sh", _arg("script", _text(_script(test.command))))] if test.command else []
    if not steps:
        warnings.append(f"{where}: v1 test has no command; the stage is empty")
    return _stage(
        name, steps, agent=AgentDocker(_text(test.image), None, SPAN), environment=environment
    )


def _env_vars(test: V1Test, where: str, warnings: list[str]) -> list[EnvVar]:
    result = []
    for name, value in test.environment.items():
        if not ENV_NAME.fullmatch(name):
            warnings.append(
                f"{where}: environment name '{name}' is not valid in v2 and was dropped"
            )
            continue
        if SENSITIVE_NAME.search(name):
            warnings.append(
                f"{where}: '{name}' looks like a credential; its v1 value was copied as a literal "
                "test value into the Jenkinsfile (verify it is a dummy, never a real secret)"
            )
        result.append(EnvVar(name, _text(value), SPAN))
    return result


def _script(command: tuple[str, ...]) -> str:
    """``sh -c <script>`` becomes the script itself; anything else is shell-quoted."""
    if len(command) == MIN_SHELL_WRAPPER_LENGTH and command[:2] == SHELL_WRAPPER:
        return command[2]
    return shlex.join(command)


def _deploy_stage(
    recipe: V1Recipe, services: list[ServiceImage], warnings: list[str]
) -> Stage | None:
    mapped = [item for item in services if item.image is not None]
    for item in services:
        if item.image is None:
            warnings.append(
                f"service '{item.service}' has no image mapping ({item.note}); it is NOT in the "
                "deploy step and must be added by hand"
            )
    steps: list[StepCall] = []
    if mapped:
        entries = tuple(MapEntry(item.service, _text(str(item.image)), SPAN) for item in mapped)
        steps.append(_call("deploy", _arg("services", MapValue(entries, SPAN))))
    steps.extend(_health_check(check, warnings) for check in recipe.checks)
    return _stage("Deploy", steps) if steps else None


def _health_check(check: V1Check, warnings: list[str]) -> StepCall:
    if len(check.statuses) > 1:
        warnings.append(
            f"health check {check.url}: v1 accepts statuses {list(check.statuses)}; v2 accepts "
            f"one, so {check.statuses[0]} was used"
        )
    return _call(
        "healthCheck",
        _arg("url", _text(check.url)),
        _arg("expectStatus", IntValue(check.statuses[0], SPAN)),
    )
