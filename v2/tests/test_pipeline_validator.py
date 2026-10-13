import pytest

from shiftcodes.domain.diagnostics import Severity
from shiftcodes.pipeline import ValidationContext, parse, validate
from shiftcodes.pipeline.docker import check_docker_args, is_external_reference
from tests.pipeline_support import CONTEXT, DOCKER_AGENT, check, errors, wrap


def only_error(text: str) -> str:
    found = errors(text)
    assert len(found) == 1, found
    return found[0]


def test_valid_minimal_has_no_diagnostics() -> None:
    assert check(wrap("sh 'make'\necho 'ok'")) == []


def test_all_errors_are_reported_and_sorted() -> None:
    text = wrap(
        "foo 'x'\nsh script: 'a', returnStdout: true\nretry(0) { }",
        agent="agent any",
        extra="environment { bad = 'x' }",
    )
    diagnostics = check(text)
    positions = [(d.span.line, d.span.column) for d in diagnostics]
    assert positions == sorted(positions)
    messages = [d.message for d in diagnostics]
    assert any("environment name 'bad'" in m for m in messages)
    assert any("unknown step 'foo'" in m for m in messages)
    assert any("returnStdout" in m for m in messages)
    assert any("between 1 and 10" in m for m in messages)
    assert any("sh runs inside a container" in m for m in messages)


@pytest.mark.parametrize(
    ("steps", "message"),
    [
        ("sh()", "requires argument 'script'"),
        ("sh script: 'a', script: 'b'", "duplicate argument 'script'"),
        ("sh 'a', 'b'", "unexpected positional argument"),
        ("sh script: 1", "must be a string"),
        ("sh script: 'a', nope: 1", "unknown argument 'nope'"),
        ("sh script: 'a', returnStatus: true", "there is no Groovy to consume the result"),
        ("echo()", "requires argument 'message'"),
        ("timeout(time: 0, unit: 'MINUTES') { }", "must be greater than 0"),
        ("timeout(time: 1, unit: 'DAYS') { }", "must be one of SECONDS, MINUTES, HOURS"),
        ("timeout(time: 1)", "needs a block"),
        ("timeout(time: '5') { }", "must be an integer"),
        ("retry(3)", "needs a block"),
        ("echo('a') { }", "does not take a block"),
        ("dir('/abs') { }", "must be relative"),
        ("dir('a/../b') { }", "must not contain '..'"),
        ('dir("${BUILD_NUMBER}") { }', "literal string without variables"),
        ("withEnv(['lower=1']) { }", "NAME=value"),
        ("withEnv(['NOEQUALS']) { }", "NAME=value"),
        ("withEnv('A=1') { }", "must be a list"),
        ("dockerBuild()", "requires argument 'image'"),
        ("dockerBuild(image: 'UPPER')", "image name 'UPPER' is not valid"),
        ("dockerBuild(image: 'a', context: '/')", "must be relative"),
        ("dockerBuild(image: 'a', dockerfile: 'x/../../y')", "must not contain '..'"),
        ("dockerBuild(image: 'a', args: [A: 1])", "valid string"),
        ("dockerBuild(image: 'a', target: '-x')", "target '-x' is not valid"),
        ("deploy service: 'a'", "deploy needs service and image"),
        ("deploy services: [a: 'x'], service: 'a'", "either service/image or services"),
        ("healthCheck(url: 'ftp://x')", "must start with http:// or https://"),
        ("healthCheck(url: 'https://x', expectStatus: 99)", "between 100 and 599"),
        ("healthCheck(url: 'https://x', timeout: 0)", "between 1 and 3600"),
        ("healthCheck()", "requires argument 'url'"),
    ],
)
def test_step_rules(steps: str, message: str) -> None:
    assert message in "\n".join(errors(wrap(steps)))


def test_unknown_step_lists_supported_steps() -> None:
    message = only_error(wrap("archive 'x'"))
    assert message.startswith("unknown step 'archive'; supported: ")
    for step in ("deploy", "dockerBuild", "healthCheck", "sh", "withEnv"):
        assert step in message


def test_valid_platform_steps() -> None:
    steps = (
        "dockerBuild(image: 'app.v1-x', dockerfile: 'docker/Dockerfile', context: 'src', "
        "target: 'prod', args: [A: 'b'])\n"
        "deploy(service: 'web', image: 'app.v1-x')\n"
        "deploy(services: [web: 'app.v1-x', 'api': 'app.v1-x'])\n"
        'healthCheck(url: "https://${ENVIRONMENT}.x/up")'
    )
    assert errors(wrap(steps)) == []


def test_sh_requires_docker_agent_at_some_level() -> None:
    message = "sh runs inside a container: set agent { docker { image '...' } }"
    assert only_error(wrap("sh 'x'", agent="agent any")) == message
    assert only_error(wrap("sh 'x'", agent="agent none")) == message
    assert errors(wrap("echo 'x'", agent="agent none")) == []
    stage_level = (
        "pipeline { agent none\n stages { stage('a') { agent { docker 'node:22' }\n"
        " steps { sh 'x' } }; stage('b') { agent any\n steps { sh 'y' } } } }"
    )
    assert errors(stage_level) == [message]
    nested = (
        "pipeline { agent { docker 'node:22' }\n stages { stage('p') { agent any\n parallel {"
        " stage('a') { steps { sh 'x' } } } } } }"
    )
    assert errors(nested) == [message]
    in_post = "pipeline { agent any\n stages { }\n post { always { sh 'x' } } }"
    assert message in errors(in_post)


def test_image_flow_and_order() -> None:
    ok = (
        "pipeline { agent any\n stages {\n"
        " stage('b') { steps { dockerBuild(image: 'app') } }\n"
        " stage('t') { agent { docker 'app' }\n steps { sh 'x' } }\n"
        " stage('d') { steps { deploy service: 's', image: 'app' } }\n } }"
    )
    assert errors(ok) == []
    early = ok.replace("docker 'app'", "docker 'other'")
    assert "not built by an earlier dockerBuild and is not a valid external" in only_error(early)
    same_stage = wrap("deploy service: 's', image: 'app'\ndockerBuild(image: 'app')", "agent any")
    assert "image 'app' is not built" in only_error(same_stage)
    inside_block = wrap("retry(2) { dockerBuild(image: 'app') }\ndeploy service: 's', image: 'app'")
    assert errors(inside_block) == []


def test_parallel_branches_do_not_see_each_other_but_later_stages_do() -> None:
    template = (
        "pipeline { agent any\n stages {\n stage('p') { parallel {\n"
        " stage('a') { steps { dockerBuild(image: 'img') } }\n"
        " stage('b') { steps { %s } }\n } }\n"
        " stage('after') { steps { deploy service: 's', image: 'img' } }\n } }"
    )
    assert errors(template % "echo 'x'") == []
    assert len(errors(template % "deploy service: 's', image: 'img'")) == 1


def test_docker_agent_image_rules() -> None:
    def agent(image: str) -> list[str]:
        return errors(wrap("echo 'x'", f"agent {{ docker '{image}' }}"))

    assert agent("node:22") == []
    assert agent("registry.example.com:5000/team/app:1.2-rc_1") == []
    assert agent("ghcr.io/o/r@sha256:" + "a" * 64) == []
    assert agent("node")  # needs a tag or digest
    assert agent("Node:22")
    assert agent("node:")
    assert agent("bad image:1")
    assert agent("node:22 --privileged")
    variable = errors(wrap("echo 'x'", 'agent { docker "node:${BRANCH_NAME}" }'))
    assert "docker image must be a literal string" in variable[0]
    assert (
        "literal string"
        in errors(wrap("echo 'x'", 'agent { docker { image "a:1"; args "-e A=${B}" } }'))[0]
    )


@pytest.mark.parametrize(
    ("args", "flag"),
    [
        ("--privileged", "--privileged"),
        ("-v /:/host", "-v"),
        ("--volume=/a:/b", "--volume"),
        ("--cap-add SYS_ADMIN", "--cap-add"),
        ("--pid host", "--pid"),
        ("--user root", "--user"),
        ("--network host", "--network"),
        ("--network=bridge", "--network"),
        ("--memory", "--memory"),
        ("-e", "-e"),
        ("-e noequals", "-e"),
        ("--cpus abc", "--cpus"),
        ("--memory 1x", "--memory"),
        ("stray", "stray"),
        ("-it", "-it"),
        ("--network none --privileged", "--privileged"),
        ("--device /dev/sda", "--device"),
        ("'unbalanced", "cannot parse"),
    ],
)
def test_docker_args_denied(args: str, flag: str) -> None:
    problem = check_docker_args(args)
    assert problem is not None
    assert flag in problem


@pytest.mark.parametrize(
    "args",
    [
        "",
        "--network none",
        "--network=none",
        "-e A=b -e C_D=e=f",
        "--memory 512m --cpus 1.5",
        "--memory=2g --cpus=2",
        "-e 'GREETING=hello world'",
    ],
)
def test_docker_args_allowed(args: str) -> None:
    assert check_docker_args(args) is None


def test_denied_args_surface_in_validation() -> None:
    text = wrap("echo 'x'", "agent { docker { image 'node:22'; args '-v /:/host' } }")
    assert "docker arg '-v' is not allowed" in only_error(text)


def test_external_reference_grammar() -> None:
    assert is_external_reference("a/b/c:1")
    assert not is_external_reference("a/b/c")
    assert not is_external_reference("-bad:1")
    assert not is_external_reference("x:" + "a" * 129)


def test_environment_rules() -> None:
    def env(text: str) -> list[str]:
        return errors(wrap("echo 'x'", "agent any", f"environment {{ {text} }}"))

    assert env("A_1 = 'x'") == []
    assert "must match" in env("a = 'x'")[0]
    assert "duplicate environment variable 'A'" in env("A = 'x'; A = 'y'")[0]
    assert env("A = 'x'; B = \"${A}\"") == []
    assert "unknown variable 'B'" in env("A = \"${B}\"; B = 'x'")[0]
    assert "unknown variable 'A'" in env('A = "${A}"')[0]


def test_credentials_must_exist() -> None:
    text = wrap("echo 'x'", "agent any", "environment { T = credentials('NPM_TOKEN') }")
    assert errors(text) == []
    assert "unknown credential 'NPM_TOKEN'" in errors(text, ValidationContext())[0]


def test_secret_interpolation_warns() -> None:
    text = wrap('sh "deploy ${T}"', extra="environment { T = credentials('NPM_TOKEN') }")
    diagnostics = check(text)
    assert [d.severity for d in diagnostics] == [Severity.WARNING]
    assert "single quotes" in diagnostics[0].message
    assert check(wrap("sh 'deploy $T'", extra="environment { T = credentials('NPM_TOKEN') }")) == []


@pytest.mark.parametrize(
    ("string", "message"),
    [
        ("${UNKNOWN}", "unknown variable 'UNKNOWN'"),
        ("$UNKNOWN", "unknown variable 'UNKNOWN'"),
        ("${env.UNKNOWN}", "unknown variable 'UNKNOWN'"),
        ("${foo()}", "unsupported expression '${foo()}'"),
        ("${1 + 2}", "unsupported expression"),
        ("${env.A.B}", "unsupported expression"),
        ("${params.NOPE}", "unknown parameter 'params.NOPE'"),
        ("${currentBuild.number}", "unsupported expression"),
        ("${}", "unsupported expression"),
    ],
)
def test_interpolation_errors(string: str, message: str) -> None:
    assert message in only_error(wrap(f'echo "{string}"'))


def test_interpolation_known_names() -> None:
    builtins = (
        "${BRANCH_NAME} ${GIT_COMMIT} ${BUILD_NUMBER} ${ENVIRONMENT} ${PROJECT} ${env.PROJECT}"
    )
    assert errors(wrap(f'echo "{builtins} $BUILD_NUMBER \\$ESCAPED"')) == []
    params = "parameters { string(name: 'V') }"
    assert errors(wrap('echo "${params.V}"', extra=params)) == []
    assert errors(wrap("echo 'single $NOT_EXPANDED ${nothing}'")) == []
    env = wrap('echo "${A}"', extra="environment { A = 'x' }")
    assert errors(env) == []


def test_with_env_binds_names_for_its_block_only() -> None:
    text = wrap('withEnv([\'A=1\']) { echo "${A}" }\necho "${A}"')
    found = errors(text)
    assert len(found) == 1
    assert "unknown variable 'A'" in found[0]


def test_stage_environment_scope() -> None:
    text = (
        "pipeline { agent any\n stages {\n"
        " stage('a') { environment { X = 'v' }\n steps { echo \"${X}\" } }\n"
        " stage('b') { steps { echo \"${X}\" } }\n } }"
    )
    found = check(text)
    assert [(d.span.line, "unknown variable 'X'" in d.message) for d in found] == [(5, True)]


def test_stage_names_must_be_unique_per_level() -> None:
    dup = (
        "pipeline { agent any\n stages {\n stage('a') { steps { echo 'x' } }\n"
        " stage('a') { steps { echo 'y' } }\n } }"
    )
    found = check(dup)
    assert [d.span.line for d in found] == [4]
    assert "first used at line 3" in found[0].message
    same_name_other_level = (
        "pipeline { agent any\n stages {\n stage('a') { parallel {\n"
        " stage('a') { steps { echo 'x' } }\n stage('b') { steps { echo 'y' } } } } } }"
    )
    assert errors(same_name_other_level) == []
    dup_branch = same_name_other_level.replace("stage('b')", "stage('a')")
    assert "duplicate stage name 'a'" in only_error(dup_branch)
    assert "must not be empty" in only_error(
        dup.replace("'a')", "' ')", 1).replace("stage('a')", "stage('z')")
    )


def test_structure_rules() -> None:
    assert "pipeline needs at least one stage" in only_error("pipeline { agent any }")
    assert "missing required 'agent'" in only_error(wrap("echo 'x'", agent=""))
    assert "steps section is empty" in only_error(wrap(""))
    empty_parallel = "pipeline { agent any\n stages { stage('a') { parallel { } } } }"
    assert "parallel needs at least one stage" in only_error(empty_parallel)
    empty_nested = "pipeline { agent any\n stages { stage('a') { stages { } } } }"
    assert "stages section is empty" in only_error(empty_nested)


def test_options_and_parameters() -> None:
    def pipeline(section: str) -> str:
        return wrap("echo 'x'", "agent any", section)

    assert errors(pipeline("options { timeout(time: 5, unit: 'SECONDS'); retry(2) }")) == []
    assert "unknown option 'buildDiscarder'" in errors(pipeline("options { buildDiscarder() }"))[0]
    assert "does not take a block" in errors(pipeline("options { retry(2) { } }"))[0]
    assert "unknown parameter type 'text'" in errors(pipeline("parameters { text(name: 'A') }"))[0]
    dup = "parameters { string(name: 'A'); string(name: 'A') }"
    assert "duplicate parameter 'A'" in errors(pipeline(dup))[0]
    assert "non-empty list" in errors(pipeline("parameters { choice(name: 'A', choices: []) }"))[0]
    assert "requires argument 'name'" in errors(pipeline("parameters { string() }"))[0]


def test_when_validation() -> None:
    def stage(when: str) -> list[str]:
        text = (
            f"pipeline {{ agent any\n stages {{ stage('a') {{ when {{ {when} }}\n"
            " steps { echo 'x' } } } }"
        )
        return errors(text)

    assert stage("branch 'release/*'; environment name: 'ENV_X', value: 'y'") == []
    assert "literal string" in stage('branch "a-${BRANCH_NAME}"')[0]
    assert "must not be empty" in stage("branch ''")[0]
    assert "is not valid" in stage("environment name: 'lower', value: 'y'")[0]
    assert "environment value" in stage("environment name: 'A', value: \"${A}\"")[0]
    assert "branch pattern" in stage("not { anyOf { branch '' } }")[0]


def test_unknown_names_in_nested_unknown_step_blocks_are_still_checked() -> None:
    found = errors(wrap('nope { echo "${X}" }'))
    assert len(found) == 2


def test_validation_is_pure_function_of_inputs() -> None:
    pipeline = parse(wrap("sh 'x'"))
    assert validate(pipeline, CONTEXT) == validate(pipeline, CONTEXT)
    assert DOCKER_AGENT in wrap("sh 'x'")


def test_duplicate_options_and_empty_post_blocks_are_errors() -> None:
    dup = wrap("echo 'x'", "agent any", "options { retry(2); retry(3) }")
    assert "duplicate option 'retry'" in only_error(dup)
    stage = (
        "pipeline { agent any\n stages { stage('a') {\n"
        " options { timeout(time: 1); timeout(time: 2) }\n steps { echo 'x' } } } }"
    )
    assert "duplicate option 'timeout'" in only_error(stage)
    other = wrap("echo 'x'", "agent any", "options { timeout(time: 1); retry(2) }")
    assert errors(other) == []
    empty = "pipeline { agent any\n stages { }\n post { always { } } }"
    assert "post condition 'always' has no steps" in errors(empty)
