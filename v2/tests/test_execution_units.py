"""Small pure pieces of execution: globs, agent args, limits, scope rendering, retry."""

from __future__ import annotations

import pytest

from shiftcodes.domain.errors import ValidationError
from shiftcodes.domain.resources import parse_cpus, parse_memory
from shiftcodes.execution.cancel import CancelReason, CancelToken
from shiftcodes.execution.conditions import glob_regex
from shiftcodes.execution.docker_agent import limited_cpus, limited_memory, parse_agent_args
from shiftcodes.execution.errors import RunCancelled, StepFailed
from shiftcodes.execution.retry import run_with_retries
from shiftcodes.execution.scope import Scope
from shiftcodes.execution.steps_docker import image_tag
from shiftcodes.pipeline import parse
from shiftcodes.pipeline.ast import Stage, StepsBody, StringValue


@pytest.mark.parametrize(
    ("pattern", "branch", "expected"),
    [
        ("main", "main", True),
        ("main", "main2", False),
        ("feature/*", "feature/login", True),
        ("feature/*", "feature/a/b", False),
        ("feature/**", "feature/a/b", True),
        ("release-?", "release-1", True),
        ("release-?", "release-10", False),
        ("v1.0", "v1x0", False),
        ("*", "main", True),
        ("*", "a/b", False),
    ],
)
def test_branch_globs(pattern: str, branch: str, expected: bool) -> None:
    assert (glob_regex(pattern).fullmatch(branch) is not None) is expected


def test_agent_args_parsing() -> None:
    args = parse_agent_args("--network none -e A=1 -e B='x y' --memory=512m --cpus 1.5")
    assert args.network_none
    assert args.env == {"A": "1", "B": "x y"}
    assert (args.memory, args.cpus) == ("512m", "1.5")
    assert parse_agent_args(None).env == {}
    with pytest.raises(StepFailed, match="not allowed"):
        parse_agent_args("--privileged")


def test_limits_may_lower_but_not_exceed() -> None:
    assert limited_memory(None, "2g") == "2g"
    assert limited_memory("1g", "2g") == "1g"
    assert limited_memory("2048m", "2g") == "2048m"
    with pytest.raises(StepFailed, match="exceeds"):
        limited_memory("2049m", "2g")
    assert limited_cpus("0.5", 2.0) == 0.5
    with pytest.raises(StepFailed, match="exceeds"):
        limited_cpus("2.5", 2.0)


def test_resource_parsing() -> None:
    assert parse_memory("512m") == 512 * 1024**2
    assert parse_memory("2G") == 2 * 1024**3
    assert parse_memory("100") == 100
    assert parse_cpus("0.5") == 0.5
    for bad in ("", "g", "-1", "1.5g", "0m"):
        with pytest.raises(ValidationError):
            parse_memory(bad)
    for bad_cpu in ("", "0", "-2", "x"):
        with pytest.raises(ValidationError):
            parse_cpus(bad_cpu)


def test_image_tag_format() -> None:
    assert (
        image_tag("shop", "production", "api", "a" * 40, 12)
        == "sc/shop-production-api:aaaaaaaaaaaa-r12"
    )


def _string(text: str) -> StringValue:
    pipeline = parse(
        f"pipeline {{\nagent none\nstages {{\nstage('S') {{\nsteps {{\necho {text}\n}}\n}}\n}}\n}}"
    )
    stage = pipeline.stages[0]
    assert isinstance(stage, Stage)
    assert isinstance(stage.body, StepsBody)
    value = stage.body.steps[0].args[0].value
    assert isinstance(value, StringValue)
    return value


def test_scope_render_substitutes_variables_without_evaluating_anything() -> None:
    scope = Scope({"A": "1", "B": "${A}"}, params={"P": "pv"})
    assert scope.render(_string('"x ${A} $B ${env.A} ${params.P}"')) == "x 1 ${A} 1 pv"
    assert scope.render(_string("'${A} literal'")) == "${A} literal"
    with pytest.raises(StepFailed, match="undefined variable 'NOPE'"):
        scope.render(_string('"${NOPE}"'))
    with pytest.raises(StepFailed, match="unknown parameter"):
        scope.render(_string('"${params.NOPE}"'))
    with pytest.raises(StepFailed, match="unsupported expression"):
        scope.render(_string('"${A.length()}"'))


def test_scope_keeps_credentials_out_of_shell_text() -> None:
    scope = Scope({"TOKEN": "s3cret", "PLAIN": "p"}, secret_names=frozenset({"TOKEN"}))
    value = _string('"curl ${TOKEN} ${PLAIN}"')
    assert scope.render(value) == "curl s3cret p"
    assert scope.render(value, for_shell=True) == "curl ${TOKEN} p"


def test_scope_directory_and_timeout_composition() -> None:
    scope = Scope({}).with_directory("a/./b").with_directory("c")
    assert scope.directory == "a/b/c"
    short = scope.with_timeout(5).with_timeout(100)
    assert short.remaining() is not None
    assert short.remaining() <= 5  # type: ignore[operator]  # an outer deadline is never extended
    assert Scope({}).remaining() is None


def test_retry_does_not_swallow_cancellation() -> None:
    calls = []

    def attempt(number: int) -> None:
        calls.append(number)
        raise RunCancelled

    with pytest.raises(RunCancelled):
        run_with_retries(5, attempt, lambda _n, _e: None)
    assert calls == [1]


def test_retry_reports_each_failed_attempt_and_raises_the_last() -> None:
    failures: list[int] = []

    def attempt(number: int) -> None:
        raise StepFailed(f"failure {number}")

    with pytest.raises(StepFailed, match="failure 3"):
        run_with_retries(3, attempt, lambda n, _e: failures.append(n))
    assert failures == [1, 2]


def test_cancel_token_first_reason_wins_and_post_phase_stops_interrupting() -> None:
    token = CancelToken()
    assert not token.is_set()
    token.cancel(CancelReason.CANCELLED)
    token.cancel(CancelReason.SHUTDOWN)
    assert token.reason is CancelReason.CANCELLED
    assert token.interrupting
    token.enter_post_phase()
    assert token.is_set()
    assert not token.interrupting
    assert token.wait(0.001)
