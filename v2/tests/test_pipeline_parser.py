import pytest

from shiftcodes.pipeline import PipelineSyntaxError, parse
from shiftcodes.pipeline.ast import (
    AgentAny,
    AgentDocker,
    AgentNone,
    AllOfCondition,
    BoolValue,
    BranchCondition,
    Credentials,
    EnvironmentCondition,
    IntValue,
    ListValue,
    MapEntry,
    MapValue,
    NotCondition,
    ParallelBody,
    PostCondition,
    SequentialBody,
    StepCall,
    StepsBody,
    StringValue,
    TextPart,
    VariablePart,
    VarSource,
)
from shiftcodes.pipeline.errors import Span
from tests.pipeline_support import parse_wrapped, wrap


def first_step(steps: str) -> StepCall:
    body = parse_wrapped(steps).stages[0].body
    assert isinstance(body, StepsBody)
    return body.steps[0]


def syntax_error(text: str) -> PipelineSyntaxError:
    with pytest.raises(PipelineSyntaxError) as info:
        parse(text)
    return info.value


def test_agents() -> None:
    def agent(text: str) -> object:
        return parse(f"pipeline {{ {text}\n stages {{ }} }}").agent

    assert isinstance(agent("agent any"), AgentAny)
    assert isinstance(agent("agent none"), AgentNone)
    short = agent("agent { docker 'node:22' }")
    assert isinstance(short, AgentDocker)
    assert short.image.plain == "node:22"
    assert short.args is None
    full = agent("agent { docker { image 'x:1'; args '--cpus 1' } }")
    assert isinstance(full, AgentDocker)
    assert (full.image.plain, full.args and full.args.plain) == ("x:1", "--cpus 1")


@pytest.mark.parametrize(
    ("fragment", "message"),
    [
        ("agent { label 'x' }", "agent type 'label' is not supported"),
        ("agent label", "agent type 'label' is not supported"),
        ("agent { docker { args '-e A=b' } }", "needs an image"),
        ("agent { docker { image 'a:1'; label 'x' } }", "option 'label' is not supported"),
        ("agent", "expected 'any'"),
    ],
)
def test_agent_errors(fragment: str, message: str) -> None:
    assert message in syntax_error(f"pipeline {{ {fragment}\n }}").message


def test_environment_values() -> None:
    pipeline = parse(
        "pipeline { environment { A = 'x'\n B = \"p ${A}\"; C = credentials('S') }; stages { } }"
    )
    a, b, c = pipeline.environment
    assert isinstance(a.value, StringValue)
    assert a.value.plain == "x"
    assert isinstance(b.value, StringValue)
    assert b.value.parts == (TextPart("p "), VariablePart(VarSource.BARE, "A", Span(1, 1)))
    assert c.value == Credentials("S", Span(1, 1))


@pytest.mark.parametrize("text", ["A = 5", "A = foo", "A 'x'", "A = credentials(B)"])
def test_environment_errors(text: str) -> None:
    syntax_error(f"pipeline {{ environment {{ {text} }} }}")


def test_option_and_parameter_calls_are_normalised() -> None:
    pipeline = parse(
        "pipeline { options { timeout(time: 5, unit: 'HOURS'); retry(2); skipDefaultCheckout() };\n"
        "parameters { choice(name: 'R', choices: ['a']) }; stages { } }"
    )
    assert [o.name for o in pipeline.options] == ["timeout", "retry", "skipDefaultCheckout"]
    assert pipeline.options[1].args[0].name == "count"
    assert pipeline.parameters[0].args[1].value == ListValue(
        (StringValue((TextPart("a"),), Span(1, 1)),), Span(1, 1)
    )


@pytest.mark.parametrize("body", ["", "cron('H * * * *')"])
def test_triggers_are_rejected_even_when_empty(body: str) -> None:
    error = syntax_error(f"pipeline {{\n triggers {{ {body} }}\n}}")
    assert "configured in the platform" in error.message
    assert error.span == Span(2, 2)


@pytest.mark.parametrize("section", ["environment", "options", "parameters", "post"])
def test_empty_sections_are_rejected_like_jenkins(section: str) -> None:
    assert f"{section} cannot be empty" in syntax_error(f"pipeline {{ {section} {{ }} }}").message


def test_stage_bodies_and_nesting() -> None:
    pipeline = parse(
        "pipeline { stages {\n"
        " stage('p') { parallel { stage('a') { steps { } };\n"
        " stage('b') { stages { stage('c') { steps { } } } } } }\n"
        " stage('s') { stages { stage('d') { steps { } } } }\n"
        "} }"
    )
    parallel = pipeline.stages[0].body
    assert isinstance(parallel, ParallelBody)
    assert isinstance(parallel.stages[1].body, SequentialBody)
    assert isinstance(pipeline.stages[1].body, SequentialBody)


@pytest.mark.parametrize(
    ("stage", "message"),
    [
        ("stage('a') { }", "a stage needs one of: steps, parallel, stages"),
        ("stage('a') { steps { }; stages { } }", "only one of"),
        ("stage('a') { steps { }; steps { } }", "duplicate section 'steps'"),
        ("stage('a') { tools { } }", "section 'tools' is not supported"),
        ("stage('a') { parallel { echo 'x' } }", "only stages are allowed here"),
        (
            "stage('a') { parallel { stage('b') { parallel { } } } }",
            "parallel cannot be nested inside another parallel",
        ),
        (
            "stage('a') { parallel { stage('b') { stages { stage('c') { parallel { } } } } } }",
            "parallel cannot be nested inside another parallel",
        ),
        ('stage("a ${B}") { steps { } }', "must not contain variables"),
        ("stage { steps { } }", "expected '('"),
    ],
)
def test_stage_errors(stage: str, message: str) -> None:
    assert message in syntax_error(f"pipeline {{ stages {{ {stage} }} }}").message


def test_when_conditions() -> None:
    pipeline = parse(
        "pipeline { stages { stage('a') { when { branch 'main'\n"
        " environment name: 'X', value: 'y'\n"
        " not { allOf { branch('a'); branch 'b' } } }; steps { } } } }"
    )
    when = pipeline.stages[0].when
    assert when is not None
    branch, env, negated = when.conditions
    assert isinstance(branch, BranchCondition)
    assert isinstance(env, EnvironmentCondition)
    assert (env.name, env.value.plain) == ("X", "y")
    assert isinstance(negated, NotCondition)
    assert isinstance(negated.condition, AllOfCondition)


@pytest.mark.parametrize(
    ("when", "message"),
    [
        ("expression { true }", "expression conditions (free Groovy) are not supported in when"),
        ("tag 'v*'", "when condition 'tag' is not supported"),
        ("not { branch 'a'; branch 'b' }", "not takes exactly one"),
        ("allOf { }", "allOf needs at least one"),
        ("branch", "branch takes one pattern"),
        ("branch pattern: 'a'", "branch takes one pattern"),
        ("environment name: 'X'", "environment condition needs"),
        ("", "when needs at least one"),
    ],
)
def test_when_errors(when: str, message: str) -> None:
    text = f"pipeline {{ stages {{ stage('a') {{ when {{ {when} }} steps {{ }} }} }} }}"
    assert message in syntax_error(text).message


def test_post_blocks_are_sorted_canonically() -> None:
    post = parse(
        "pipeline { post { success { }; always { }; cleanup { }; failure { } }; stages { } }"
    ).post
    assert post is not None
    assert [b.condition for b in post.blocks] == [
        PostCondition.ALWAYS,
        PostCondition.FAILURE,
        PostCondition.SUCCESS,
        PostCondition.CLEANUP,
    ]


@pytest.mark.parametrize(
    ("post", "message"),
    [
        ("fixed { }", "post condition 'fixed' is not supported"),
        ("always { }; always { }", "duplicate post"),
    ],
)
def test_post_errors(post: str, message: str) -> None:
    assert message in syntax_error(f"pipeline {{ post {{ {post} }} }}").message


def test_call_syntaxes_are_equivalent() -> None:
    assert first_step("sh 'x'") == first_step("sh(script: 'x')") == first_step("sh script: 'x'")
    assert first_step("sh 'x'").args[0].name == "script"
    block = first_step("retry(3) {\n echo 'a'\n }")
    assert block.args[0].value == IntValue(3, Span(1, 1))
    assert block.body is not None
    assert block.body[0].name == "echo"
    assert first_step("deploy service: 'a',\n image: 'b'").args[1].name == "image"
    assert first_step("unknown 'x', 'y'").args[0].name is None


def test_values() -> None:
    step = first_step("dockerBuild image: 'a', args: [A: 'b', 'C-D': \"x\"]")
    args = step.args[1].value
    assert isinstance(args, MapValue)
    assert [e.key for e in args.entries] == ["A", "C-D"]
    assert isinstance(args.entries[0], MapEntry)
    assert first_step("deploy services: [:]").args[0].value == MapValue((), Span(1, 1))
    assert first_step("withEnv([]) { }").args[0].value == ListValue((), Span(1, 1))
    flag = first_step("sh script: 'x', returnStatus: true").args[1].value
    assert flag == BoolValue(True, Span(1, 1))
    assert first_step("withEnv(['A=1',\n 'B=2',\n]) { }").args[0].value.items[1].plain == "B=2"  # type: ignore[union-attr]


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        ("script { echo 'x' }", "script blocks (free Groovy) are not supported; use sh steps"),
        ("def x = 1", "Groovy 'def' is not supported"),
        ("x = 1", "assignments are not supported"),
        ("if (true) { }", "Groovy 'if' is not supported"),
        ("try { }", "Groovy 'try' is not supported"),
        ("sh foo", "'foo' is not a value"),
        ("sh null", "null is not supported"),
        ("sh env.FOO", "'env' is not a value"),
        ("dir 'x' { }", "put the arguments in parentheses"),
        ("sh 'a' echo 'b'", "expected a new line or ';'"),
        ("sh(script: 'a'", "expected ',' or ')'"),
        ("sh [a: 1, 2]", "map keys must be identifiers or strings"),
        ("sh [1 2]", "expected ',' or ']'"),
        ("retry(3) { echo 'a'", "unclosed '{'"),
        ("sh 'a'; ; echo 'b'\n }}} {", "expected"),
    ],
)
def test_step_syntax_errors(steps: str, message: str) -> None:
    assert message in syntax_error(wrap(steps)).message


def test_error_position_inside_steps() -> None:
    text = wrap("echo 'a'\nsh script: 'b'  oops")
    error = syntax_error(text)
    assert error.span.line == 8
    assert "oops" in str(error)
    assert str(error).endswith("^")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "single pipeline"),
        ("stages { }", "single pipeline"),
        ("def x = 1\npipeline { }", "outside pipeline"),
        ("class A { }\npipeline { }", "outside pipeline"),
        ("pipeline { }\npipeline { }", "code after the pipeline"),
        ("pipeline { } echo 'x'", "code after the pipeline"),
        ("pipeline", "expected '{'"),
        ("@Library('x') _\npipeline { }", "@Library"),
        ("pipeline { agent any\n agent none }", "duplicate section 'agent'"),
        ("pipeline { tools { } }", "section 'tools' is not supported in pipeline"),
        ("pipeline { script { } }", "script blocks"),
    ],
)
def test_top_level_errors(text: str, message: str) -> None:
    assert message in syntax_error(text).message


def test_comments_are_ignored_everywhere() -> None:
    text = "// lead\npipeline { /* a */ agent any // t\n stages { // s\n } }\n// end"
    assert isinstance(parse(text).agent, AgentAny)


def test_arbitrary_sequential_nesting_and_parallel_in_sequential() -> None:
    deep = "stage('d') { steps { } }"
    for name in "cba":
        deep = f"stage('{name}') {{ stages {{ {deep} }} }}"
    assert parse(f"pipeline {{ stages {{ {deep} }} }}").stages
    nested = "stage('s') { stages { stage('p') { parallel { stage('x') { steps { } } } } } }"
    assert parse(f"pipeline {{ stages {{ {nested} }} }}").stages


def test_newline_allowed_before_section_braces() -> None:
    text = (
        "pipeline\n{\n agent\n {\n docker\n {\n image 'x:1'\n }\n }\n stages\n {\n"
        " stage('a')\n {\n steps\n {\n echo 'x'\n }\n }\n }\n}\n"
    )
    assert isinstance(parse(text).agent, AgentDocker)
    assert "unclosed" not in str(parse(text).stages)


def test_newlines_inside_parentheses_of_credentials_and_stage_names() -> None:
    text = (
        "pipeline { environment { A = credentials(\n 'X'\n ) }\n"
        " stages { stage(\n 'a'\n ) { steps { echo 'x' } } } }"
    )
    pipeline = parse(text)
    assert pipeline.environment[0].value == Credentials("X", Span(1, 1))
    assert pipeline.stages[0].name == "a"


def test_timeout_accepts_a_positional_time() -> None:
    step = first_step("timeout(5) { echo 'x' }")
    assert step.args[0].name == "time"
    assert first_step("timeout(time: 5) { echo 'x' }") == step


def test_block_after_steps_call_must_share_the_line() -> None:
    assert "expected a step name" in syntax_error(wrap("retry(3)\n{ echo 'x' }")).message
