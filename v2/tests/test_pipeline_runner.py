"""PipelineRunner behaviour with a fake container runtime."""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path

from sqlalchemy import Engine

from shiftcodes.adapters.deployer import UnavailableDeployer
from shiftcodes.domain.actor import Actor
from shiftcodes.domain.enums import EnvironmentName, RunStatus
from shiftcodes.execution.config import ExecutionConfig
from shiftcodes.execution.step_registry import STEP_CLASSES
from shiftcodes.pipeline.steps import STEPS
from shiftcodes.repositories.models import Run
from shiftcodes.services.audit import AuditService
from shiftcodes.services.environments import EnvironmentService, EnvironmentUpdate
from shiftcodes.services.secret_cipher import SecretCipher
from shiftcodes.services.secrets import SecretService
from tests.conftest import DEV
from tests.execution_support import SHA, Behavior, FakeSource, Harness, make_harness

AGENT = "agent { docker 'alpine:3.20' }"
OK = RunStatus.SUCCEEDED
FAIL = RunStatus.FAILED
SKIP = RunStatus.SKIPPED


def pipeline(stages: str, *, agent: str = AGENT, head: str = "", post: str = "") -> str:
    """A Jenkinsfile around ``stages``."""
    return f"pipeline {{\n{agent}\n{head}\nstages {{\n{stages}\n}}\n{post}\n}}\n"


def stage(name: str, steps: str, extra: str = "") -> str:
    """A stage with a steps block."""
    return f"stage('{name}') {{\n{extra}\nsteps {{\n{steps}\n}}\n}}\n"


def test_step_registry_covers_the_catalogue() -> None:
    assert set(STEP_CLASSES) == set(STEPS)


def test_stages_run_in_order_and_are_recorded(harness: Harness) -> None:
    text = pipeline(
        stage("A", "sh 'echo A'")
        + stage("B", "echo 'hello'\nsh 'echo B'")
        + stage("C", "sh 'echo C'")
    )
    run, outcome = harness.run(text)
    assert outcome.status is OK
    assert harness.runtime.script_order == ["echo A", "echo B", "echo C"]
    assert harness.stage_statuses(run) == {"Checkout": OK, "A": OK, "B": OK, "C": OK}
    assert harness.steps(run, "B") == [("echo: hello", OK), ("sh: echo B", OK)]
    assert harness.source.requests[0].commit_sha == SHA
    final = harness.detail(run).run
    assert final.status is OK
    assert final.started_at is not None
    assert final.finished_at is not None


def test_workspace_is_removed_after_the_run(harness: Harness) -> None:
    run, _ = harness.run(pipeline(stage("A", "sh 'true'")))
    assert not harness.env.workspaces.path(run.id).exists()


def test_failing_step_fails_stage_and_run_and_skips_later_stages(harness: Harness) -> None:
    harness.runtime.when("boom", exit_code=3)
    text = pipeline(
        stage("A", "sh 'echo fine'")
        + stage("B", "sh 'boom'\nsh 'never'")
        + stage("C", "sh 'later'"),
        post="post {\nfailure { echo 'post ran' }\nalways { echo 'always ran' }\n}",
    )
    run, outcome = harness.run(text)
    assert outcome.status is FAIL
    assert outcome.message is not None
    assert "stage 'B' failed" in outcome.message
    assert "exited with code 3" in outcome.message
    statuses = harness.stage_statuses(run)
    assert statuses["A"] is OK
    assert statuses["B"] is FAIL
    assert statuses["C"] is SKIP
    assert "never" not in harness.runtime.script_order
    assert harness.steps(run, "B") == [("sh: boom", FAIL)]
    assert harness.detail(run).stages[-1].stage.name == "Post Actions"
    assert statuses["Post Actions"] is OK
    assert harness.detail(run).run.message == outcome.message
    exit_codes = [s.exit_code for d in harness.detail(run).stages for s in d.steps]
    assert 3 in exit_codes


def test_error_step_fails_with_message(harness: Harness) -> None:
    run, outcome = harness.run(pipeline(stage("A", "error 'custom failure'")))
    assert outcome.status is FAIL
    assert outcome.message is not None
    assert "custom failure" in outcome.message
    assert "ERROR: custom failure" in harness.log_of(run, "A")


def test_checkout_failure_fails_the_run_and_skips_stages(
    tmp_path: Path, engine: Engine, cipher: SecretCipher, project: str
) -> None:
    local = make_harness(tmp_path, engine, cipher, project, source=FakeSource(error="no such ref"))
    run, outcome = local.run(
        pipeline(stage("A", "sh 'x'"), post="post {\nalways { echo 'cleanup done' }\n}")
    )
    assert outcome.status is FAIL
    assert outcome.message is not None
    assert outcome.message.startswith("checkout failed: no such ref")
    statuses = local.stage_statuses(run)
    assert statuses["Checkout"] is FAIL
    assert statuses["A"] is SKIP
    assert statuses["Post Actions"] is OK
    assert not local.runtime.runs


# --- when ----------------------------------------------------------------------------


def test_when_branch_environment_and_combinators(harness: Harness) -> None:
    gated = (
        stage("prod only", "echo 'p'", "when { branch 'production' }")
        + stage("feature", "echo 'f'", "when { branch 'feature/*' }")
        + stage(
            "dev env", "echo 'd'", "when { environment name: 'ENVIRONMENT', value: 'development' }"
        )
        + stage("not prod", "echo 'n'", "when { not { branch 'production' } }")
        + stage(
            "all",
            "echo 'a'",
            f"when {{ allOf {{ branch 'dev*'; {ENV_DEV} }} }}",
        )
        + stage("any", "echo 'y'", "when { anyOf { branch 'production'; branch 'development' } }")
        + stage("none", "echo 'z'", "when { anyOf { branch 'production'; branch 'main' } }")
    )
    run, outcome = harness.run(pipeline(gated), branch="development")
    assert outcome.status is OK
    statuses = harness.stage_statuses(run)
    assert [name for name, status in statuses.items() if status is SKIP] == [
        "prod only",
        "feature",
        "none",
    ]
    assert all(statuses[n] is OK for n in ("dev env", "not prod", "all", "any"))


def test_branch_glob_does_not_cross_slashes(harness: Harness) -> None:
    text = pipeline(
        stage("one level", "echo 'x'", "when { branch 'feature/*' }")
        + stage("any depth", "echo 'y'", "when { branch 'feature/**' }")
    )
    run, _ = harness.run(text, branch="feature/a/b")
    statuses = harness.stage_statuses(run)
    assert statuses["one level"] is SKIP
    assert statuses["any depth"] is OK


# --- parallel and nested stages ----------------------------------------------------------


def test_parallel_branches_run_concurrently_up_to_the_bound(harness: Harness) -> None:
    harness.runtime.when("slow", delay=0.2)
    parallel = "parallel {\n" + "".join(stage(n, f"sh 'slow {n}'") for n in "abc") + "}\n"
    text = pipeline("stage('Fan out') {\n" + parallel + "}\n" + stage("After", "sh 'after'"))
    run, outcome = harness.run(text)
    assert outcome.status is OK
    assert harness.runtime.max_concurrent == 2  # default bound
    statuses = harness.stage_statuses(run)
    assert statuses["Fan out / a"] is OK
    assert statuses["Fan out / c"] is OK
    assert statuses["Fan out"] is OK
    assert harness.runtime.script_order[-1] == "after"


def test_parallel_failure_does_not_stop_siblings_but_skips_later_stages(harness: Harness) -> None:
    harness.runtime.when("bad", exit_code=1)
    parallel = (
        "parallel {\n"
        + stage("x", "sh 'bad'")
        + stage("y", "sh 'good y'")
        + stage("z", "sh 'good z'")
        + "}\n"
    )
    text = pipeline("stage('P') {\n" + parallel + "}\n" + stage("After", "sh 'after'"))
    run, outcome = harness.run(text)
    assert outcome.status is FAIL
    statuses = harness.stage_statuses(run)
    assert statuses["P / x"] is FAIL
    assert statuses["P / y"] is OK
    assert statuses["P / z"] is OK  # not skipped just because a sibling failed
    assert statuses["P"] is FAIL
    assert statuses["After"] is SKIP


def test_nested_sequential_stages(harness: Harness) -> None:
    harness.runtime.when("bad", exit_code=1)
    inner = (
        "stages {\n"
        + stage("one", "sh 'one'")
        + stage("two", "sh 'bad'")
        + stage("three", "sh 'three'")
        + "}\n"
    )
    run, outcome = harness.run(pipeline("stage('Outer') {\n" + inner + "}\n"))
    assert outcome.status is FAIL
    statuses = harness.stage_statuses(run)
    assert statuses["Outer / one"] is OK
    assert statuses["Outer / two"] is FAIL
    assert statuses["Outer / three"] is SKIP
    assert statuses["Outer"] is FAIL


# --- post ----------------------------------------------------------------------------------

ENV_DEV = "environment name: 'ENVIRONMENT', value: 'development'"
POST_ALL = """post {
    cleanup { echo 'cleanup' }
    success { echo 'success' }
    failure { echo 'failure' }
    always { echo 'always' }
    aborted { echo 'aborted' }
    unstable { echo 'unstable' }
    changed { echo 'changed' }
}"""
POST_CHANGED = "post {\nchanged { echo 'changed' }\n}"


def _post_echoes(harness: Harness, run: Run) -> list[str]:
    stages = harness.detail(run).stages
    post = next(d for d in stages if d.stage.name == "Post Actions")
    return [s.label.removeprefix("echo: ") for s in post.steps]


def test_post_success_order_and_selection(harness: Harness) -> None:
    run, outcome = harness.run(pipeline(stage("A", "sh 'ok'"), post=POST_ALL))
    assert outcome.status is OK
    assert _post_echoes(harness, run) == ["always", "success", "cleanup"]


def test_post_failure_selection(harness: Harness) -> None:
    harness.runtime.when("bad", exit_code=1)
    run, outcome = harness.run(pipeline(stage("A", "sh 'bad'"), post=POST_ALL))
    assert outcome.status is FAIL
    assert _post_echoes(harness, run) == ["always", "failure", "cleanup"]


def test_post_changed_compares_with_previous_run_of_the_environment(harness: Harness) -> None:
    harness.runtime.when("bad", exit_code=1)
    post = POST_CHANGED
    first, _ = harness.run(pipeline(stage("A", "sh 'bad'"), post=post))
    assert _post_echoes(harness, first) == []  # no previous run: nothing changed
    second, outcome = harness.run(pipeline(stage("A", "sh 'ok'"), post=post))
    assert outcome.status is OK
    assert _post_echoes(harness, second) == ["changed"]  # failed -> succeeded
    third, _ = harness.run(pipeline(stage("A", "sh 'ok'"), post=post))
    assert _post_echoes(harness, third) == []  # same as before


def test_post_changed_ignores_other_environments(harness: Harness) -> None:
    harness.runtime.when("bad", exit_code=1)
    post = POST_CHANGED
    harness.run(pipeline(stage("A", "sh 'bad'"), post=post), environment=EnvironmentName.PRODUCTION)
    run, _ = harness.run(pipeline(stage("A", "sh 'ok'"), post=post))
    assert _post_echoes(harness, run) == []


def test_stage_level_post_and_cleanup_after_failure(harness: Harness) -> None:
    harness.runtime.when("bad", exit_code=1)
    post = (
        "post {\nfailure { echo 'stage failed' }\nsuccess { echo 'never' }\n"
        "cleanup { echo 'tidy' }\n}"
    )
    text = pipeline("stage('S') {\nsteps {\nsh 'bad'\n}\n" + post + "\n}\n")
    run, outcome = harness.run(text)
    assert outcome.status is FAIL
    labels = [label for label, _ in harness.steps(run, "S")]
    assert labels == ["sh: bad", "echo: stage failed", "echo: tidy"]
    assert harness.stage_statuses(run)["S"] is FAIL


def test_failing_post_step_fails_the_run_but_cleanup_still_runs(harness: Harness) -> None:
    post = "post {\nalways { error 'post broke' }\ncleanup { echo 'still here' }\n}"
    run, outcome = harness.run(pipeline(stage("A", "sh 'ok'"), post=post))
    assert outcome.status is FAIL
    assert outcome.message is not None
    assert "post always failed" in outcome.message
    assert "still here" in harness.all_logs(run)


# --- options: timeout and retry ------------------------------------------------------------


def test_timeout_step_kills_a_slow_script(harness: Harness) -> None:
    harness.runtime.when("sleep", block=True)
    text = pipeline(
        stage("A", "timeout(time: 1, unit: 'SECONDS') {\nsh 'sleep 100'\n}") + stage("B", "sh 'b'")
    )
    started = time.monotonic()
    run, outcome = harness.run(text)
    assert time.monotonic() - started < 10
    assert outcome.status is FAIL
    assert outcome.message is not None
    assert "timed out" in outcome.message
    assert harness.stage_statuses(run)["B"] is SKIP


def test_stage_timeout_option(harness: Harness) -> None:
    harness.runtime.when("sleep", block=True)
    text = pipeline(stage("A", "sh 'sleep 100'", "options { timeout(time: 1, unit: 'SECONDS') }"))
    _, outcome = harness.run(text)
    assert outcome.status is FAIL


def test_retry_step_retries_until_success(harness: Harness) -> None:
    harness.runtime.rules.append(
        (
            "flaky",
            __import__("tests.execution_support", fromlist=["Behavior"]).Behavior(
                sequence=[1, 1, 0]
            ),
        )
    )
    run, outcome = harness.run(pipeline(stage("A", "retry(3) {\nsh 'flaky'\n}")))
    assert outcome.status is OK
    assert harness.runtime.script_order.count("flaky") == 3
    labels = [label for label, _ in harness.steps(run, "A")]
    assert labels == ["retry: 3 attempts", "sh: flaky", "sh: flaky", "sh: flaky"]
    assert "attempt 1 failed" in harness.log_of(run, "A", 0)


def test_retry_gives_up_after_the_last_attempt(harness: Harness) -> None:
    harness.runtime.when("always-bad", exit_code=2)
    run, outcome = harness.run(pipeline(stage("A", "retry(2) {\nsh 'always-bad'\n}")))
    assert outcome.status is FAIL
    assert harness.runtime.script_order.count("always-bad") == 2
    assert harness.stage_statuses(run)["A"] is FAIL


def test_retry_option_on_a_stage(harness: Harness) -> None:
    harness.runtime.rules.append(("flaky", Behavior(sequence=[1, 0])))
    text = pipeline(stage("A", "sh 'flaky'", "options { retry(2) }"))
    _run, outcome = harness.run(text)
    assert outcome.status is OK
    assert outcome.message is None
    assert harness.runtime.script_order.count("flaky") == 2


def test_pipeline_retry_option_restarts_stages(harness: Harness) -> None:
    harness.runtime.rules.append(("flaky", Behavior(sequence=[1, 0])))
    text = pipeline(stage("A", "sh 'a'") + stage("B", "sh 'flaky'"), head="options { retry(2) }")
    run, outcome = harness.run(text)
    assert outcome.status is OK
    assert harness.runtime.script_order == ["a", "flaky", "a", "flaky"]
    assert harness.stage_statuses(run)["B"] is OK


# --- interpolation and credentials ----------------------------------------------------------


def test_interpolation_uses_the_resolved_environment(harness: Harness) -> None:
    head = "environment {\nGREETING = 'hi'\nFULL = \"${GREETING} there\"\n}"
    steps = (
        'echo "${FULL} on ${BRANCH_NAME} build ${BUILD_NUMBER} ${env.GREETING} $PROJECT"\n'
        'sh "echo ${FULL} ${ENVIRONMENT}"\n'
        "sh 'echo $GREETING'"
    )
    run, outcome = harness.run(pipeline(stage("A", steps), head=head))
    assert outcome.status is OK
    assert harness.log_of(run, "A", 0) == "hi there on development build 1 hi demo\n"
    assert harness.runtime.script_order[:2] == ["echo hi there development", "echo $GREETING"]
    env = harness.runtime.runs[0].env
    assert env["GREETING"] == "hi"
    assert env["FULL"] == "hi there"
    assert env["GIT_COMMIT"] == SHA


def test_with_env_adds_variables_inside_the_block(harness: Harness) -> None:
    steps = "withEnv(['MODE=fast', 'X=1']) {\nsh \"echo ${MODE}\"\n}\nsh 'echo done'"
    run, outcome = harness.run(pipeline(stage("A", steps)))
    assert outcome.status is OK
    first, second = harness.runtime.runs
    assert first.env["MODE"] == "fast"
    assert "MODE" not in second.env
    assert harness.runtime.script_order[0] == "echo fast"
    assert harness.steps(run, "A")[0][0] == "withEnv: MODE, X"


def test_credentials_are_resolved_masked_and_kept_out_of_argv(harness: Harness) -> None:
    secret = "s3cr3t-value-123"
    harness.add_secret("API_TOKEN", secret)
    harness.runtime.when("print", lines=(f"token is {secret}", "ok"))
    head = "environment {\nTOKEN = credentials('API_TOKEN')\n}"
    steps = (
        'echo "echoing ${TOKEN}"\n'
        "sh 'print $TOKEN'\n"
        'sh "print ${TOKEN}"\n'
        "withEnv(['X=1']) {\nsh 'print again'\n}"
    )
    run, outcome = harness.run(pipeline(stage("A", steps), head=head))
    assert outcome.status is OK
    assert secret not in harness.all_logs(run)
    assert "[REDACTED]" in harness.log_of(run, "A", 1)
    assert harness.log_of(run, "A", 0) == "echoing [REDACTED]\n"
    for spec in harness.runtime.runs:
        assert spec.env["TOKEN"] == secret  # reaches the container through the env file only
        assert secret not in " ".join(spec.command)
    assert harness.runtime.script_order[:2] == ["print $TOKEN", "print ${TOKEN}"]
    # nothing persisted outside the container environment contains the secret
    detail = harness.detail(run)
    assert secret not in (detail.run.message or "")
    assert all(secret not in s.label for d in detail.stages for s in d.steps)
    events = AuditService(harness.uow()).list(action="secret.resolve")
    assert len(events) == 1
    assert secret not in str(events[0].details)
    assert events[0].details["names"] == ["API_TOKEN"]


def test_credentials_in_a_stage_environment(harness: Harness) -> None:
    harness.add_secret("NPM_TOKEN", "npm-secret-xyz")
    harness.runtime.when("go", lines=("using npm-secret-xyz",))
    stage_text = stage("A", "sh 'go'", "environment { NPM = credentials('NPM_TOKEN') }")
    run, outcome = harness.run(pipeline(stage_text))
    assert outcome.status is OK
    assert harness.log_of(run, "A").splitlines()[-2:] == [
        "running in alpine:3.20",
        "using [REDACTED]",
    ]


def test_multiline_secret_is_masked_line_by_line(harness: Harness) -> None:
    key = "-----BEGIN KEY-----\nabcdefghij123456\n-----END KEY-----"
    harness.add_secret("PRIVATE", key)
    harness.runtime.when("dump", lines=tuple(key.splitlines()))
    head = "environment {\nKEY = credentials('PRIVATE')\n}"
    run, _ = harness.run(pipeline(stage("A", "sh 'dump'"), head=head))
    log = harness.log_of(run, "A")
    assert "abcdefghij123456" not in log
    assert "BEGIN KEY" not in log


def test_deleting_a_secret_after_queueing_fails_the_run_cleanly(harness: Harness) -> None:
    harness.add_secret("GONE", "value-gone-123")
    head = "environment {\nX = credentials('GONE')\n}"
    run = harness.queue(pipeline(stage("A", "sh 'x'"), head=head))
    SecretService(harness.uow(), harness.cipher).delete("demo", DEV, "GONE", Actor.system("t"))
    outcome = harness.execute(run)
    assert outcome.status is FAIL
    assert outcome.message is not None
    assert "unknown credential 'GONE'" in outcome.message


def test_invalid_pipeline_text_fails_the_run(harness: Harness) -> None:
    run, outcome = harness.run("pipeline { nonsense }")
    assert outcome.status is FAIL
    assert outcome.message is not None
    assert "pipeline syntax error" in outcome.message
    assert [d.stage.name for d in harness.detail(run).stages] == ["Pipeline"]
    assert "pipeline syntax error" in harness.log_of(run, "Pipeline")


# --- logs ----------------------------------------------------------------------------------


def test_log_is_truncated_once_at_the_bound(
    tmp_path: Path, engine: Engine, cipher: SecretCipher, project: str
) -> None:
    config = ExecutionConfig(log_max_bytes=300, cancel_poll_interval=0.05)
    local = make_harness(tmp_path, engine, cipher, project, config=config)
    local.runtime.when("spam", lines=tuple(f"line number {i:04d}" for i in range(100)))
    run, outcome = local.run(pipeline(stage("A", "sh 'spam'")))
    assert outcome.status is OK
    log = local.log_of(run, "A")
    assert log.count("output truncated") == 1
    assert log.rstrip().endswith("]")
    assert len(log.encode()) < 300 + 200
    assert "line number 0099" not in log


def test_log_files_are_private(harness: Harness) -> None:
    run, _ = harness.run(pipeline(stage("A", "sh 'x'")))
    step = harness.detail(run).stages[1].steps[0]
    path = harness.env.logs.path(run.id, step.id)
    assert path.stat().st_mode & 0o777 == 0o640
    assert path.parent.stat().st_mode & 0o777 == 0o750


# --- deploy and health check -----------------------------------------------------------------


def test_deploy_receives_the_built_image_tags(harness: Harness) -> None:
    EnvironmentService(harness.uow()).update(
        "demo",
        DEV,
        EnvironmentUpdate(compose_project="demo-dev", compose_files=["/srv/demo/compose.yml"]),
    )
    steps = "dockerBuild(image: 'web')\ndeploy(service: 'web', image: 'web')"
    run, outcome = harness.run(pipeline(stage("Ship", steps), agent="agent none"))
    assert outcome.status is OK
    request = harness.deployer.requests[0]
    assert request.project == "demo"
    assert request.compose_project == "demo-dev"
    assert request.compose_files == ("/srv/demo/compose.yml",)
    assert [(s.service, s.image) for s in request.services] == [
        ("web", "sc/demo-development-web:0123456789ab-r1")
    ]
    assert request.run_id == run.id


def test_unavailable_deployer_fails_the_step(harness: Harness) -> None:
    harness.env = replace(harness.env, deployer=UnavailableDeployer())
    steps = "dockerBuild(image: 'web')\ndeploy(service: 'web', image: 'web')"
    run, outcome = harness.run(pipeline(stage("Ship", steps), agent="agent none"))
    assert outcome.status is FAIL
    assert outcome.message is not None
    assert "deploy is not available yet (Task 7)" in outcome.message
    assert "deploy is not available yet (Task 7)" in harness.log_of(run, "Ship", 1)


def test_health_check_retries_until_the_expected_status(harness: Harness) -> None:
    harness.probe.results = [None, 503, 200]
    steps = "healthCheck(url: 'http://app.test/up', timeout: 5)"
    run, outcome = harness.run(pipeline(stage("H", steps), agent="agent none"))
    assert outcome.status is OK
    assert len(harness.probe.calls) == 3
    log = harness.log_of(run, "H")
    assert "connection refused" in log
    assert "got status 503, expected 200" in log
    assert "health check passed: 200" in log


def test_health_check_expect_status_and_failure(harness: Harness) -> None:
    harness.probe.results = [204]
    steps = "healthCheck(url: 'http://app.test/up', expectStatus: 204, timeout: 2)"
    _, outcome = harness.run(pipeline(stage("H", steps), agent="agent none"))
    assert outcome.status is OK
    harness.probe.results = [500]
    steps = "healthCheck(url: 'http://app.test/up', timeout: 1)"
    _, failed = harness.run(pipeline(stage("H", steps), agent="agent none"))
    assert failed.status is FAIL
    assert failed.message is not None
    assert "health check failed: got status 500, expected 200" in failed.message


# --- cancellation ----------------------------------------------------------------------------


def test_cancel_stops_between_steps_kills_containers_and_runs_post(harness: Harness) -> None:
    harness.runtime.when("long", block=True)
    post = (
        "post {\naborted { echo 'aborted ran' }\ncleanup { echo 'cleanup ran' }\n"
        "failure { echo 'no' }\n}"
    )
    text = pipeline(stage("A", "sh 'long'\nsh 'second step'") + stage("B", "sh 'b'"), post=post)
    run = harness.queue(text)
    results: list[RunStatus] = []

    def work() -> None:
        results.append(harness.execute(run).status)

    thread = threading.Thread(target=work)
    thread.start()
    deadline = time.monotonic() + 10
    while not harness.runtime.running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert harness.runtime.running, "the long step never started"
    harness.env.lifecycle().request_cancel(run.id, Actor.system("test"))
    thread.join(10)
    assert not thread.is_alive()
    assert results == [RunStatus.CANCELLED]
    assert any(label.startswith("shiftcodes.run_id=") for label in harness.runtime.kills)
    assert "second step" not in harness.runtime.script_order
    assert "b" not in harness.runtime.script_order
    statuses = harness.stage_statuses(run)
    assert statuses["A"] is RunStatus.CANCELLED
    assert statuses["B"] is SKIP
    assert statuses["Post Actions"] is OK
    log = harness.all_logs(run)
    assert "aborted ran" in log
    assert "cleanup ran" in log
    assert "no" not in [line for line in log.splitlines() if line == "no"]
    final = harness.detail(run).run
    assert final.status is RunStatus.CANCELLED
    assert final.cancel_requested_at is not None
    assert not harness.env.workspaces.path(run.id).exists()
