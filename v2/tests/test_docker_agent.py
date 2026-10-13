"""Image, mount, workdir, network and limit choices of container steps."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import Engine

from shiftcodes.adapters.workspace import WorkspaceManager
from shiftcodes.domain.enums import RunStatus
from shiftcodes.execution.config import ExecutionConfig
from shiftcodes.services.environments import EnvironmentService, EnvironmentUpdate
from shiftcodes.services.secret_cipher import SecretCipher
from tests.conftest import DEV
from tests.execution_support import Harness, make_harness
from tests.test_pipeline_runner import AGENT, pipeline, stage

TAG = "sc/demo-development-app:0123456789ab-r1"
BUILD = "dockerBuild(image: 'app', dockerfile: 'Dockerfile', target: 'prod', args: [MODE: 'fast'])"


def _external_run(harness: Harness, steps: str, agent: str = AGENT) -> RunStatus:
    _, outcome = harness.run(pipeline(stage("S", steps), agent=agent))
    return outcome.status


def test_docker_build_tags_labels_and_limits(harness: Harness) -> None:
    run, outcome = harness.run(pipeline(stage("Build", BUILD), agent="agent none"))
    assert outcome.status is RunStatus.SUCCEEDED
    spec = harness.runtime.builds[0]
    assert spec.tag == TAG
    assert spec.target == "prod"
    assert spec.build_args == {"MODE": "fast"}
    assert spec.memory == "2g"
    assert spec.labels == {
        "shiftcodes.project": "demo",
        "shiftcodes.environment": "development",
        "shiftcodes.run_id": str(run.id),
        "shiftcodes.commit": "0123456789abcdef0123456789abcdef01234567",
    }
    workspace = harness.env.workspaces.path(run.id)
    assert spec.dockerfile == workspace.resolve() / "Dockerfile"
    assert spec.context == workspace.resolve()


def test_logical_image_runs_with_its_own_workdir_and_no_workspace(harness: Harness) -> None:
    text = pipeline(
        stage("Build", BUILD) + stage("Test", "sh 'run tests'", "agent { docker 'app' }"),
        agent="agent none",
    )
    _, outcome = harness.run(text)
    assert outcome.status is RunStatus.SUCCEEDED
    spec = harness.runtime.runs[0]
    assert spec.image == TAG
    assert spec.workdir is None
    assert spec.mounts == ()
    assert spec.user is None
    assert harness.runtime.pulls == []  # never pulled: it is a local build
    assert spec.entrypoint == "sh"
    assert spec.command == ("-c", "run tests")


def test_logical_image_with_dir_changes_directory_inside_the_image(harness: Harness) -> None:
    steps = "dir('backend') {\nsh 'make'\n}"
    text = pipeline(
        stage("Build", BUILD) + stage("T", steps, "agent { docker 'app' }"), agent="agent none"
    )
    harness.run(text)
    spec = harness.runtime.runs[0]
    assert spec.workdir is None
    assert spec.command[1] == "cd -- backend || exit 1\nmake"


def test_external_image_is_pulled_and_mounts_the_workspace(harness: Harness) -> None:
    run, outcome = harness.run(pipeline(stage("S", "sh 'node -v'")))
    assert outcome.status is RunStatus.SUCCEEDED
    assert harness.runtime.pulls == ["alpine:3.20"]
    spec = harness.runtime.runs[0]
    assert spec.image == "alpine:3.20"
    assert [(m.source, m.target, m.read_only) for m in spec.mounts] == [
        (harness.env.workspaces.host_path(run.id), "/workspace", False)
    ]
    assert spec.workdir == "/workspace"
    assert spec.user == f"{os.getuid()}:{os.getgid()}"
    assert spec.env["HOME"] == "/tmp"  # noqa: S108


def test_external_image_is_pulled_once_per_run_and_not_when_present(harness: Harness) -> None:
    harness.run(pipeline(stage("S", "sh 'one'\nsh 'two'")))
    assert harness.runtime.pulls == ["alpine:3.20"]
    harness.run(pipeline(stage("S", "sh 'three'")))
    assert harness.runtime.pulls == ["alpine:3.20"]  # present locally now


def test_dir_is_relative_to_the_workspace_mount(harness: Harness) -> None:
    steps = "dir('app') {\ndir('src') {\nsh 'ls'\n}\n}"
    harness.run(pipeline(stage("S", steps)))
    spec = harness.runtime.runs[0]
    assert spec.workdir == "/workspace/app/src"
    assert spec.command[1] == "ls"  # no cd prelude: the workdir does it


def test_every_container_has_labels_name_and_cpu_shares(harness: Harness) -> None:
    run, _ = harness.run(pipeline(stage("S", "sh 'a'\nsh 'b'")))
    first, second = harness.runtime.runs
    step_ids = [s.id for s in harness.detail(run).stages[1].steps]
    assert first.labels == {
        "shiftcodes.run_id": str(run.id),
        "shiftcodes.step_id": str(step_ids[0]),
    }
    assert second.labels["shiftcodes.step_id"] == str(step_ids[1])
    assert first.name != second.name
    assert first.name.startswith(f"sc-r{run.id}-s{step_ids[0]}-")
    assert first.cpu_shares == 512


def _compose(harness: Harness) -> None:
    update = EnvironmentUpdate(compose_project="demo-dev")
    EnvironmentService(harness.uow()).update("demo", DEV, update)


def _network_run(harness: Harness, args: str = "") -> str | None:
    agent = f"agent {{ docker {{ image 'alpine:3.20'\nargs '{args}' }} }}" if args else AGENT
    harness.run(pipeline(stage("S", "sh 'x'"), agent=agent))
    return harness.runtime.runs[-1].network


def test_network_defaults_to_the_environment_compose_network(harness: Harness) -> None:
    assert _network_run(harness) is None  # no compose project: Docker's default bridge
    EnvironmentService(harness.uow()).update(
        "demo", DEV, EnvironmentUpdate(compose_project="demo-dev")
    )
    assert _network_run(harness) is None  # network not created yet: falls back, with a notice
    harness.runtime.networks.add("demo-dev_default")
    assert _network_run(harness) == "demo-dev_default"


def test_missing_compose_network_is_reported_in_the_log(harness: Harness) -> None:
    EnvironmentService(harness.uow()).update(
        "demo", DEV, EnvironmentUpdate(compose_project="demo-dev")
    )
    run, _ = harness.run(pipeline(stage("S", "sh 'x'")))
    assert "network demo-dev_default does not exist yet" in harness.log_of(run, "S")


def test_network_none_isolates_even_with_a_compose_network(harness: Harness) -> None:
    EnvironmentService(harness.uow()).update(
        "demo", DEV, EnvironmentUpdate(compose_project="demo-dev")
    )
    harness.runtime.networks.add("demo-dev_default")
    assert _network_run(harness, "--network none") == "none"


def test_allowlisted_args_are_applied(harness: Harness) -> None:
    args = "-e FOO=bar --memory 512m --cpus 1.5"
    assert _network_run(harness, args) is None
    spec = harness.runtime.runs[-1]
    assert spec.env["FOO"] == "bar"
    assert spec.memory == "512m"
    assert spec.cpus == 1.5


def test_default_limits_come_from_settings(harness: Harness) -> None:
    _network_run(harness)
    spec = harness.runtime.runs[-1]
    assert (spec.memory, spec.cpus) == ("2g", 2.0)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ("--memory 4g", "--memory 4g exceeds the configured maximum of 2g"),
        ("--memory 3000m", "exceeds the configured maximum"),
        ("--cpus 3", "--cpus 3 exceeds the configured maximum of 2"),
    ],
)
def test_pipeline_limits_may_not_exceed_the_configured_maxima(
    harness: Harness, args: str, message: str
) -> None:
    agent = f"agent {{ docker {{ image 'alpine:3.20'\nargs '{args}' }} }}"
    _, outcome = harness.run(pipeline(stage("S", "sh 'x'"), agent=agent))
    assert outcome.status is RunStatus.FAILED
    assert outcome.message is not None
    assert message in outcome.message
    assert harness.runtime.runs == []


def test_limits_follow_configuration(
    tmp_path: Path, engine: Engine, cipher: SecretCipher, project: str
) -> None:
    config = ExecutionConfig(container_memory="256m", container_cpus=0.5, build_memory="1g")
    local = make_harness(tmp_path, engine, cipher, project, config=config)
    local.run(pipeline(stage("B", BUILD), agent="agent none"))
    assert local.runtime.builds[0].memory == "1g"
    local.run(pipeline(stage("S", "sh 'x'")))
    spec = local.runtime.runs[0]
    assert (spec.memory, spec.cpus) == ("256m", 0.5)


def test_sh_without_an_agent_container_is_refused_at_run_time(harness: Harness) -> None:
    # the validator rejects this text, so the run fails before any container is started
    _, outcome = harness.run(pipeline(stage("S", "sh 'x'"), agent="agent none"))
    assert outcome.status is RunStatus.FAILED
    assert "sh runs inside a container" in (outcome.message or "")
    assert harness.runtime.runs == []


def test_workspace_over_the_size_bound_fails_the_run(
    tmp_path: Path, engine: Engine, cipher: SecretCipher, project: str
) -> None:
    local = make_harness(tmp_path, engine, cipher, project)
    local.env = replace(
        local.env, workspaces=WorkspaceManager(tmp_path / "data" / "workspaces", max_bytes=1000)
    )
    local.source.files = {"big.bin": "x" * 20000}
    run, outcome = local.run(pipeline(stage("S", "sh 'x'")))
    assert outcome.status is RunStatus.FAILED
    assert outcome.message is not None
    assert outcome.message.startswith("checkout failed: workspace is")
    assert not (tmp_path / "data" / "workspaces" / str(run.id)).exists()
