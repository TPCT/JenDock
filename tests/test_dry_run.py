"""The dry run plans a pipeline for a branch without executing anything."""

from __future__ import annotations

from backend.execution.dry_run import DryRun, DryStage, StageMode, plan_dry_run
from backend.pipeline import parse
from tests.pipeline_support import FIXTURES

NEXTJS = (FIXTURES / "valid" / "nextjs.groovy").read_text()

PIPELINE = """
pipeline {
    agent { docker { image 'node:22' } }
    environment {
        TARGET = 'staging'
        TOKEN = credentials('NPM_TOKEN')
    }
    stages {
        stage('Build') {
            steps {
                sh 'npm ci'
                timeout(time: 5, unit: 'MINUTES') {
                    sh(script: 'npm run build', label: 'compile')
                }
            }
        }
        stage('Release') {
            when { branch 'release/*' }
            stages {
                stage('Tag') { steps { echo 'tag' } }
                stage('Publish') { parallel { stage('A') { steps { echo 'a' } } } }
            }
        }
        stage('Staging only') {
            when { environment name: 'TARGET', value: 'staging' }
            steps { echo "to ${TARGET}" }
        }
        stage('Never') {
            when { not { branch '*' } }
            steps { echo 'x' }
        }
    }
    post { failure { echo 'failed' } }
}
"""


def plan(text: str, branch: str, environment: str = "production") -> DryRun:
    return plan_dry_run(parse(text), "shop", environment, branch)


def by_path(run: DryRun) -> dict[str, tuple[bool, str | None]]:
    found: dict[str, tuple[bool, str | None]] = {}

    def walk(stages: tuple[DryStage, ...]) -> None:
        for stage in stages:
            found[stage.path] = (stage.will_run, stage.skip_reason)
            walk(stage.stages)

    walk(run.stages)
    return found


def test_stages_run_or_skip_for_the_branch() -> None:
    main = by_path(plan(PIPELINE, "main"))
    assert main["Build"] == (True, None)
    assert main["Staging only"] == (True, None)  # an environment-level literal is known
    assert main["Release"][0] is False
    assert "does not match branch 'main'" in str(main["Release"][1])
    assert main["Never"][0] is False
    release = by_path(plan(PIPELINE, "release/1.2"))
    assert release["Release"] == (True, None)
    assert release["Release / Tag"] == (True, None)
    assert release["Release / Publish / A"] == (True, None)


def test_children_of_a_skipped_stage_are_skipped_with_a_reason() -> None:
    found = by_path(plan(PIPELINE, "main"))
    assert found["Release / Tag"] == (False, "an enclosing stage does not run")
    assert found["Release / Publish / A"][0] is False


def test_steps_are_listed_with_the_labels_of_a_real_run() -> None:
    build = plan(PIPELINE, "main").stages[0]
    assert build.mode is StageMode.STEPS
    assert [(s.name, s.label) for s in build.steps] == [
        ("sh", "sh: npm ci"),
        ("timeout", "timeout: 5 MINUTES"),
    ]
    inner = build.steps[1].children
    assert [(s.name, s.label) for s in inner] == [("sh", "sh: compile")]


def test_agents_post_and_modes_are_reported() -> None:
    result = plan(PIPELINE, "main")
    assert result.agent == "docker node:22"
    assert [(b.condition, [s.label for s in b.steps]) for b in result.post] == [
        ("failure", ["echo: failed"])
    ]
    modes = {stage.name: stage.mode for stage in result.stages}
    assert modes["Release"] is StageMode.SEQUENTIAL
    assert result.stages[1].stages[1].mode is StageMode.PARALLEL
    assert (result.branch, result.environment) == ("main", "production")


def test_builtin_environment_variable_drives_environment_conditions() -> None:
    result = plan(NEXTJS, "main", environment="development")
    release = by_path(result)["Release"]
    assert release == (True, None)  # branch main and ENVIRONMENT is not 'staging'
    assert by_path(plan(NEXTJS, "feature/x"))["Release"][0] is False


def test_credentials_are_never_read() -> None:
    # TOKEN is a credential: it must not appear as a known variable, so a condition on it fails.
    text = PIPELINE.replace(
        "when { environment name: 'TARGET', value: 'staging' }",
        "when { environment name: 'TOKEN', value: '' }",
    )
    assert by_path(plan(text, "main"))["Staging only"][0] is False
