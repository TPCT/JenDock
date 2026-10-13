"""The Compose deployer against a real Docker daemon (``make test-docker``).

Each test starts a throwaway Compose project ``sctest-<random>`` with two services (``web`` has
a healthcheck, ``worker`` does not). Every container and image carries ``shiftcodes.test=1``
and a label with the stack's own name; teardown removes exactly those and the project's
network, and the fixture verifies that nothing is left.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from shiftcodes.adapters.compose_deployer import ComposeDeployer, DeployConfig
from shiftcodes.adapters.container_runtime import CancelSignal, ExecResult, LineSink
from shiftcodes.adapters.deploy_health import HealthPolicy
from shiftcodes.adapters.deploy_journal import DeployJournal
from shiftcodes.adapters.deployer import DeployRequest, ServiceImage
from shiftcodes.adapters.docker_commands import CommandRunner, DockerCommands
from shiftcodes.adapters.errors import DeployError
from shiftcodes.adapters.override_store import OverrideStore
from shiftcodes.domain.enums import DeploymentStatus
from tests.deploy_support import MemoryLedger, NotCancelled
from tests.docker_support import docker_available, pick_base_image, remove_image
from tests.execution_support import FakeProbe

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not docker_available(), reason="no Docker daemon available"),
]
PROJECT = "demo"
ENVIRONMENT = "development"
REPOSITORY = f"sc/{PROJECT}-{ENVIRONMENT}"
POLICY = HealthPolicy(timeout=60.0, settle=2.0, interval=0.5)
PLATFORM_LABELS = (f"shiftcodes.project={PROJECT}", f"shiftcodes.environment={ENVIRONMENT}")


class Crash(BaseException):
    """Raised to stop the deployer the way a dead process would."""


class CrashAfterUp:
    """A ``CommandRunner`` that dies on the first query after ``compose up`` finished."""

    def __init__(self, inner: CommandRunner) -> None:
        self._inner = inner
        self._up_done = False

    def capture(
        self, args: Sequence[str], timeout: float = 60.0
    ) -> subprocess.CompletedProcess[str]:
        if self._up_done:
            raise Crash
        return self._inner.capture(args, timeout)

    def stream(
        self,
        args: Sequence[str],
        sink: LineSink,
        *,
        timeout: float | None,
        cancel: CancelSignal | None = None,
    ) -> ExecResult:
        result = self._inner.stream(args, sink, timeout=timeout, cancel=cancel)
        self._up_done = self._up_done or "up" in args
        return result


@dataclass(slots=True)
class Stack:
    """A throwaway compose project and the images it uses."""

    name: str
    base: str
    directory: Path
    docker: DockerCommands
    ledger: MemoryLedger
    runs: int = 0

    # --- plumbing -----------------------------------------------------------------

    @property
    def own_labels(self) -> tuple[str, ...]:
        """Labels of everything this stack creates: the test label and its own name."""
        return ("shiftcodes.test=1", f"shiftcodes.stack={self.name}")

    @property
    def compose_file(self) -> Path:
        return self.directory / "compose.yml"

    @property
    def deployments(self) -> Path:
        return self.directory / "deployments"

    @property
    def override_path(self) -> Path:
        return self.deployments / self.name / "override.yml"

    def run(self, *args: str, timeout: float = 180) -> subprocess.CompletedProcess[str]:
        return self.docker.capture(list(args), timeout)

    def compose(self, *args: str, override: bool = False) -> subprocess.CompletedProcess[str]:
        files = ["-f", str(self.compose_file)]
        if override:
            files += ["-f", str(self.override_path)]
        return self.run("compose", "-p", self.name, *files, *args)

    # --- images -------------------------------------------------------------------

    def build_image(self, tag: str, labels: tuple[str, ...], *extra: str) -> None:
        """Build a small image from the base with ``labels``."""
        lines = [f"FROM {self.base}"]
        for label in (*labels, *self.own_labels):
            key, _, value = label.partition("=")
            lines.append(f'LABEL {key}="{value}"')
        lines += list(extra)
        result = subprocess.run(  # noqa: S603 - test helper with fixed argv
            ["docker", "build", "--quiet", "--tag", tag, "-"],  # noqa: S607
            input="\n".join(lines) + "\n",
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    def build(self, service: str, version: int, *, healthy: bool = True) -> str:
        """Build ``sc/demo-development-<service>:v<version>``."""
        tag = f"{REPOSITORY}-{service}:v{version}"
        touch = "RUN touch /ok" if healthy else "RUN true"
        self.build_image(tag, PLATFORM_LABELS, f"LABEL version={version}", touch, "CMD sleep 3600")
        return tag

    def status(self) -> DeploymentStatus:
        """Status of the newest deployment record."""
        return self.ledger.last.status

    def image_id(self, tag: str) -> str:
        """Image id of a tag, ``""`` when it does not exist."""
        return self.run("image", "inspect", "--format", "{{.Id}}", tag).stdout.strip()

    # --- containers ---------------------------------------------------------------

    def write_compose(self, web: str, worker: str) -> None:
        labels = f'{{"shiftcodes.test": "1", "shiftcodes.stack": "{self.name}"}}'
        self.compose_file.write_text(
            f"""services:
  web:
    image: {web}
    labels: {labels}
    stop_signal: SIGKILL
    healthcheck:
      test: ["CMD", "test", "-f", "/ok"]
      interval: 1s
      timeout: 2s
      retries: 2
  worker:
    image: {worker}
    labels: {labels}
    stop_signal: SIGKILL
"""
        )

    def container(self, service: str) -> str:
        return self.compose("ps", "--all", "--quiet", service).stdout.strip()

    def inspect(self, service: str, template: str) -> str:
        return self.run("inspect", "--format", template, self.container(service)).stdout.strip()

    def image_of(self, service: str) -> str:
        """The image id the service's container runs."""
        return self.inspect(service, "{{.Image}}")

    # --- deploying ----------------------------------------------------------------

    def deployer(self, docker: CommandRunner | None = None, keep: int = 5) -> ComposeDeployer:
        return ComposeDeployer(
            docker or self.docker,
            self.ledger,
            self.deployments,
            FakeProbe([200]),
            config=DeployConfig(health=POLICY, keep_images=keep),
        )

    def request(self, service: str, image: str) -> DeployRequest:
        self.runs += 1
        return DeployRequest(
            project=PROJECT,
            environment=ENVIRONMENT,
            compose_project=self.name,
            compose_files=(str(self.compose_file),),
            services=(ServiceImage(service, image),),
            run_id=self.runs,
            commit_sha="b" * 40,
        )

    def deploy(self, service: str, image: str, deployer: ComposeDeployer | None = None) -> None:
        sink: Callable[[str], None] = lambda _line: None  # noqa: E731
        (deployer or self.deployer()).deploy(self.request(service, image), sink, NotCancelled())

    # --- cleanup ------------------------------------------------------------------

    def owned(self, kind: str) -> list[str]:
        """Ids of this stack's containers (``container``) or images (``image``)."""
        listing = ["ps", "--all"] if kind == "container" else ["image", "ls", "--all"]
        args = [*listing, "--quiet", "--filter", f"label=shiftcodes.stack={self.name}"]
        return sorted(set(self.run(*args).stdout.split()))

    def teardown(self) -> None:
        """Remove exactly what this stack created."""
        self.compose("down", "--volumes", "--remove-orphans", "--timeout", "1")
        containers = self.owned("container")
        if containers:
            self.run("rm", "--force", *containers)
        images = self.owned("image")
        if images:
            self.run("rmi", "--force", *images)


@pytest.fixture(scope="module")
def base_image() -> Iterator[str]:
    image, pulled = pick_base_image()
    yield image
    if pulled:
        remove_image(image)


@pytest.fixture
def stack(tmp_path: Path, base_image: str) -> Iterator[Stack]:
    name = f"sctest-{uuid.uuid4().hex[:8]}"
    built = Stack(name, base_image, tmp_path, DockerCommands(), MemoryLedger())
    try:
        yield built
    finally:
        built.teardown()
        networks = built.run("network", "ls", "--quiet", "--filter", f"name={name}")
        assert networks.stdout.split() == [], "compose network was not removed"
        assert built.owned("container") == []
        assert built.owned("image") == []


@pytest.fixture
def running(stack: Stack) -> Stack:
    """The stack with ``web:v1`` and ``worker:v1`` running."""
    web, worker = stack.build("web", 1), stack.build("worker", 1)
    stack.write_compose(web, worker)
    up = stack.compose("up", "--detach")
    assert up.returncode == 0, up.stderr
    return stack


def test_deploys_a_new_image_to_one_service_only(running: Stack) -> None:
    stack = running
    worker_container = stack.container("worker")
    worker_started = stack.inspect("worker", "{{.State.StartedAt}}")
    web_v1 = stack.image_of("web")
    web_v2 = stack.build("web", 2)

    stack.deploy("web", web_v2)

    assert stack.image_of("web") == stack.image_id(web_v2)
    assert stack.image_of("web") != web_v1
    assert stack.container("worker") == worker_container
    assert stack.inspect("worker", "{{.State.StartedAt}}") == worker_started
    row = stack.ledger.last
    assert row.status is DeploymentStatus.SUCCEEDED
    assert row.changes[0].previous_image == f"{REPOSITORY}-web:v1"
    assert row.changes[0].previous_image_id == web_v1
    assert row.changes[0].new_image_id == stack.image_id(web_v2)
    assert json.loads(stack.override_path.read_text()) == {"services": {"web": {"image": web_v2}}}
    # The override persists for restarts and recreations of that service.
    recreate = stack.compose(
        "up", "--detach", "--no-deps", "--force-recreate", "web", override=True
    )
    assert recreate.returncode == 0, recreate.stderr
    assert stack.image_of("web") == stack.image_id(web_v2)


def test_failing_healthcheck_rolls_back_to_the_previous_image(running: Stack) -> None:
    stack = running
    web_v1 = stack.image_of("web")
    worker_container = stack.container("worker")
    bad = stack.build("web", 2, healthy=False)

    with pytest.raises(DeployError, match="rolled back to the previous images"):
        stack.deploy("web", bad)

    assert stack.image_of("web") == web_v1
    assert stack.container("worker") == worker_container
    assert stack.inspect("web", "{{.State.Status}}") == "running"
    assert stack.status() is DeploymentStatus.ROLLED_BACK
    assert OverrideStore(stack.deployments).current(stack.name) == {}
    assert not DeployJournal(stack.deployments).exists(stack.name)


def test_retention_removes_only_old_labelled_images(running: Stack) -> None:
    stack = running
    tags = [f"{REPOSITORY}-web:v1", *(stack.build("web", n) for n in range(2, 6))]
    foreign = f"sctest-foreign-{uuid.uuid4().hex[:6]}:1"
    other_env = f"sc/{PROJECT}-production-web:v1"
    stack.build_image(foreign, (), "RUN touch /other")
    other_labels = (f"shiftcodes.project={PROJECT}", "shiftcodes.environment=production")
    stack.build_image(other_env, other_labels, "RUN touch /other")

    stack.deploy("web", tags[4], stack.deployer(keep=2))

    # v5 (deployed) and v4 are the two newest; v1 is the previous image of the last
    # deployment, so it is protected although it is older; v2 and v3 go.
    assert stack.image_id(tags[4]) and stack.image_id(tags[3])
    assert stack.image_id(tags[0])
    assert stack.image_id(tags[1]) == ""
    assert stack.image_id(tags[2]) == ""
    assert stack.image_id(foreign)
    assert stack.image_id(other_env)
    assert stack.image_id(f"{REPOSITORY}-worker:v1")


def test_crash_in_the_middle_is_rolled_back_by_recovery(running: Stack) -> None:
    stack = running
    web_v1 = stack.image_of("web")
    worker_container = stack.container("worker")
    web_v2 = stack.build("web", 2)
    crashing = stack.deployer(CrashAfterUp(stack.docker))

    with pytest.raises(Crash):
        stack.deploy("web", web_v2, crashing)

    # the worker "died" after compose up: the new image runs, the journal and a deploying
    # record remain
    assert stack.image_of("web") == stack.image_id(web_v2)
    assert DeployJournal(stack.deployments).exists(stack.name)
    assert stack.status() is DeploymentStatus.DEPLOYING

    handled = stack.deployer().recover()

    assert handled == [stack.ledger.last.id]
    assert stack.image_of("web") == web_v1
    assert stack.container("worker") == worker_container
    assert stack.status() is DeploymentStatus.ROLLED_BACK
    assert "interrupted" in (stack.ledger.last.message or "")
    assert not DeployJournal(stack.deployments).exists(stack.name)
    assert stack.deployer().recover() == []
