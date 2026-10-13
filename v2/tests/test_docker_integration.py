"""End-to-end pipeline runs against a real Docker daemon.

Run with ``make test-docker``. Skipped when Docker or git is unavailable. Every container and
image created here is labelled ``shiftcodes.test=1`` and removed in the teardown, which also
verifies that nothing is left behind.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import Engine

from shiftcodes.adapters.docker_cli import DockerCliRuntime
from shiftcodes.adapters.sources import LocalGitSource
from shiftcodes.domain.actor import Actor
from shiftcodes.domain.enums import RunStatus
from shiftcodes.execution.cancel import CancelToken
from shiftcodes.execution.config import ExecutionConfig
from shiftcodes.execution.runner import RunOutcome
from shiftcodes.repositories.models import Run
from shiftcodes.services.secret_cipher import SecretCipher
from tests.docker_support import (
    TEST_LABEL,
    assert_nothing_left,
    docker_available,
    pick_base_image,
    remove_image,
    remove_test_resources,
    running_with_label,
)
from tests.execution_support import Harness, make_harness
from tests.git_support import GIT, commit

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not docker_available(), reason="no Docker daemon available"),
    pytest.mark.skipif(GIT is None, reason="git is not installed"),
]
OK = RunStatus.SUCCEEDED
SECRET = "integration-secret-value-9f3a"
MEMORY_LIMIT = (
    "cat /sys/fs/cgroup/memory.max 2>/dev/null || cat /sys/fs/cgroup/memory/memory.limit_in_bytes"
)


@pytest.fixture(scope="module")
def base_image() -> Iterator[str]:
    image, pulled = pick_base_image()
    yield image
    if pulled:
        remove_image(image)


@pytest.fixture(autouse=True)
def _clean_docker() -> Iterator[None]:
    remove_test_resources()
    yield
    remove_test_resources()
    assert_nothing_left()


class Live:
    """A harness wired to the real docker runtime and a real local git repository."""

    def __init__(self, harness: Harness, tmp_path: Path, base: str) -> None:
        self.base = base
        self.tmp_path = tmp_path
        self.repo = tmp_path / "repo"
        self.private = tmp_path / "private"
        runtime = DockerCliRuntime(private_dir=self.private, extra_labels={"shiftcodes.test": "1"})
        source = LocalGitSource(lambda _repository: str(self.repo))
        config = ExecutionConfig(cancel_poll_interval=0.2, health_interval=0.1)
        self.harness = harness
        self.harness.env = replace(harness.env, runtime=runtime, source=source, config=config)

    def commit_repo(self, jenkinsfile: str) -> str:
        """Create the project repository: a tiny image and the Jenkinsfile."""
        dockerfile = (
            f"FROM {self.base}\nWORKDIR /app\nCOPY marker.txt /app/marker.txt\n"
            'CMD ["cat", "/app/marker.txt"]\n'
        )
        files = {
            "Dockerfile": dockerfile,
            "marker.txt": "built-marker\n",
            "README.md": "workspace-readme\n",
            "Jenkinsfile": jenkinsfile,
        }
        return commit(self.repo, files)

    def queue(self, jenkinsfile: str) -> Run:
        """Commit the repository and queue a run for that commit."""
        sha = self.commit_repo(jenkinsfile)
        return self.harness.queue(jenkinsfile, commit_sha=sha)

    def run(self, jenkinsfile: str) -> tuple[Run, RunOutcome]:
        """Queue and execute synchronously."""
        run = self.queue(jenkinsfile)
        return run, self.harness.execute(run)


@pytest.fixture
def live(
    tmp_path: Path, engine: Engine, cipher: SecretCipher, project: str, base_image: str
) -> Live:
    harness = make_harness(tmp_path, engine, cipher, project)
    return Live(harness, tmp_path, base_image)


def test_build_run_in_built_image_external_image_parallel_secret_and_isolation(
    live: Live,
) -> None:
    live.harness.add_secret("API_TOKEN", SECRET)
    base = live.base
    jenkinsfile = f"""pipeline {{
    agent none
    environment {{
        TOKEN = credentials('API_TOKEN')
    }}
    stages {{
        stage('Build') {{
            steps {{
                dockerBuild(image: 'app')
            }}
        }}
        stage('Built image') {{
            agent {{ docker 'app' }}
            steps {{
                sh 'cat /app/marker.txt && pwd && (test -e /workspace || echo no-workspace)'
            }}
        }}
        stage('External image') {{
            agent {{ docker '{base}' }}
            steps {{
                sh 'cat README.md && pwd && touch made-by-container'
                dir('sub') {{
                    sh 'pwd'
                }}
                sh 'echo "token=$TOKEN"'
            }}
        }}
        stage('Parallel') {{
            parallel {{
                stage('left') {{
                    agent {{ docker '{base}' }}
                    steps {{
                        sh 'sleep 1; echo left-done'
                    }}
                }}
                stage('right') {{
                    agent {{ docker '{base}' }}
                    steps {{
                        sh 'sleep 1; echo right-done'
                    }}
                }}
            }}
        }}
        stage('Limits') {{
            agent {{
                docker {{
                    image '{base}'
                    args '--network none --memory 256m'
                }}
            }}
            steps {{
                sh 'wget -T 2 -qO- http://1.1.1.1 >/dev/null 2>&1 || echo isolated'
                sh '{MEMORY_LIMIT}'
            }}
        }}
    }}
}}
"""
    run, outcome = live.run(jenkinsfile)
    harness = live.harness
    assert outcome.status is OK, outcome.message
    statuses = harness.stage_statuses(run)
    assert all(status is OK for status in statuses.values()), statuses
    assert set(statuses) >= {"Build", "Built image", "External image", "Parallel / left", "Limits"}
    # built image: its own WORKDIR, content from the build, no workspace mount
    built = harness.log_of(run, "Built image")
    assert "built-marker" in built
    assert "/app" in built
    assert "no-workspace" in built
    # external image: workspace mounted at /workspace, dir() relative to it
    external = harness.log_of(run, "External image")
    assert "workspace-readme" in external
    assert "/workspace\n" in external
    assert harness.log_of(run, "External image", 2).strip().splitlines()[-1] == "/workspace/sub"
    # secret: reached the container, masked in the log, never in the persisted run
    token_log = harness.log_of(run, "External image", 3)
    assert "token=[REDACTED]" in token_log
    assert SECRET not in harness.all_logs(run)
    assert SECRET not in (harness.detail(run).run.message or "")
    # parallel branches and isolation / memory limit
    assert "left-done" in harness.all_logs(run)
    assert "right-done" in harness.all_logs(run)
    assert "isolated" in harness.log_of(run, "Limits", 0)
    assert "268435456" in harness.log_of(run, "Limits", 1)
    # cleanup: workspace (including files the container created) and env files are gone
    assert not harness.env.workspaces.path(run.id).exists()
    assert list(live.private.glob("env-*")) == []


def test_failing_stage_runs_post_failure_and_skips_the_rest(live: Live) -> None:
    jenkinsfile = f"""pipeline {{
    agent {{ docker '{live.base}' }}
    stages {{
        stage('Boom') {{
            steps {{
                sh 'echo before; exit 3'
            }}
        }}
        stage('Never') {{
            steps {{
                sh 'echo never'
            }}
        }}
    }}
    post {{
        failure {{
            echo 'post failure ran'
        }}
        success {{
            echo 'post success ran'
        }}
    }}
}}
"""
    run, outcome = live.run(jenkinsfile)
    harness = live.harness
    assert outcome.status is RunStatus.FAILED
    assert outcome.message is not None
    assert "exited with code 3" in outcome.message
    statuses = harness.stage_statuses(run)
    assert statuses["Boom"] is RunStatus.FAILED
    assert statuses["Never"] is RunStatus.SKIPPED
    logs = harness.all_logs(run)
    assert "before" in logs
    assert "post failure ran" in logs
    assert "post success ran" not in logs
    assert harness.detail(run).stages[1].steps[0].exit_code == 3


def test_timeout_kills_the_container(live: Live) -> None:
    jenkinsfile = f"""pipeline {{
    agent {{ docker '{live.base}' }}
    stages {{
        stage('Slow') {{
            steps {{
                timeout(time: 2, unit: 'SECONDS') {{
                    sh 'sleep 120'
                }}
            }}
        }}
    }}
}}
"""
    started = time.monotonic()
    run, outcome = live.run(jenkinsfile)
    assert time.monotonic() - started < 40
    assert outcome.status is RunStatus.FAILED
    assert "timed out" in (outcome.message or "")
    assert running_with_label(f"shiftcodes.run_id={run.id}") == []


def test_cancel_kills_a_long_running_container_within_seconds(live: Live) -> None:
    jenkinsfile = f"""pipeline {{
    agent {{ docker '{live.base}' }}
    stages {{
        stage('Long') {{
            steps {{
                sh 'sleep 300'
                sh 'echo second step'
            }}
        }}
        stage('After') {{
            steps {{
                sh 'echo after'
            }}
        }}
    }}
    post {{
        aborted {{
            echo 'aborted post ran'
        }}
    }}
}}
"""
    run = live.queue(jenkinsfile)
    outcomes: list[RunOutcome] = []
    thread = threading.Thread(
        target=lambda: outcomes.append(live.harness.execute(run, CancelToken()))
    )
    thread.start()
    label = f"shiftcodes.run_id={run.id}"
    deadline = time.monotonic() + 90
    while not running_with_label(label) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert running_with_label(label), "the container never started"
    assert running_with_label(TEST_LABEL)  # labelled for cleanup as well
    cancelled_at = time.monotonic()
    live.harness.env.lifecycle().request_cancel(run.id, Actor.system("integration"))
    thread.join(60)
    assert not thread.is_alive()
    assert time.monotonic() - cancelled_at < 20  # "within a few seconds" plus docker teardown
    assert outcomes[0].status is RunStatus.CANCELLED
    assert running_with_label(label) == []
    logs = live.harness.all_logs(run)
    assert "second step" not in logs
    assert "after" not in logs.replace("aborted post", "")
    assert "aborted post ran" in logs
    assert live.harness.detail(run).run.status is RunStatus.CANCELLED
