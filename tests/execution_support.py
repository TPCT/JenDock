"""Fakes and builders for pipeline execution tests."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import Engine

from backend.adapters.container_runtime import (
    BuildSpec,
    CancelSignal,
    ExecResult,
    LineSink,
    RunSpec,
)
from backend.adapters.deployer import DeployRequest
from backend.adapters.errors import AdapterError, DeployError
from backend.adapters.sources import CheckoutRequest
from backend.adapters.workspace import WorkspaceManager
from backend.domain.actor import Actor
from backend.domain.enums import EnvironmentName, PipelineSource, RunStatus, RunTrigger
from backend.execution.cancel import CancelToken
from backend.execution.config import ExecutionConfig
from backend.execution.environment import ExecutionEnv
from backend.execution.logs import LogStore
from backend.execution.run_executor import RunExecutor
from backend.execution.runner import RunOutcome
from backend.repositories.database import UnitOfWork, create_session_factory
from backend.repositories.models import Run
from backend.services.run_queries import RunDetail, RunQueryService
from backend.services.runs import RunService
from backend.services.secret_cipher import SecretCipher
from backend.services.secrets import SecretService

SHA = "0123456789abcdef0123456789abcdef01234567"
POLL = 0.01


@dataclass(slots=True)
class Behavior:
    """What the fake container does for a script containing a given text."""

    lines: tuple[str, ...] = ()
    binary_chunks: tuple[bytes, ...] = ()
    exit_code: int = 0
    block: bool = False
    delay: float = 0.0
    sequence: list[int] = field(default_factory=list)  # exit codes consumed one per run


class FakeContainerRuntime:
    """Records every call; containers finish instantly unless a rule says otherwise."""

    def __init__(self) -> None:
        self.runs: list[RunSpec] = []
        self.builds: list[BuildSpec] = []
        self.pulls: list[str] = []
        self.kills: list[str] = []
        self.removed: list[str] = []
        self.images: set[str] = set()
        self.networks: set[str] = set()
        self.build_exit_code = 0
        self.pull_exit_code = 0
        self.rules: list[tuple[str, Behavior]] = []
        self.running: dict[str, RunSpec] = {}
        self.killed: set[str] = set()
        self.concurrent = 0
        self.max_concurrent = 0
        self.script_order: list[str] = []
        self._lock = threading.Lock()

    def when(self, needle: str, **kwargs: object) -> None:
        """Script containing ``needle`` behaves as ``Behavior(**kwargs)``."""
        self.rules.append((needle, Behavior(**kwargs)))  # type: ignore[arg-type]

    def _behavior(self, script: str) -> Behavior:
        for needle, behavior in self.rules:
            if needle in script:
                return behavior
        return Behavior()

    def build_image(
        self,
        spec: BuildSpec,
        sink: LineSink,
        *,
        timeout: float | None = None,
        cancel: CancelSignal | None = None,
    ) -> ExecResult:
        """Record the build."""
        with self._lock:
            self.builds.append(spec)
        sink(f"Step 1/1 : FROM {spec.tag}")
        if self.build_exit_code == 0:
            self.images.add(spec.tag)
        return ExecResult(self.build_exit_code)

    def pull_image(
        self,
        image: str,
        sink: LineSink,
        *,
        timeout: float | None = None,
        cancel: CancelSignal | None = None,
    ) -> ExecResult:
        """Record the pull."""
        with self._lock:
            self.pulls.append(image)
            if self.pull_exit_code == 0:
                self.images.add(image)
        sink("sha256:fake")
        return ExecResult(self.pull_exit_code)

    def image_exists(self, image: str) -> bool:
        """Whether the image was built or pulled."""
        return image in self.images

    def network_exists(self, name: str) -> bool:
        """Whether the network was declared."""
        return name in self.networks

    def run_container(
        self,
        spec: RunSpec,
        sink: LineSink,
        *,
        timeout: float | None = None,
        cancel: CancelSignal | None = None,
    ) -> ExecResult:
        """Run the scripted behaviour for the spec's script."""
        script = spec.command[-1] if spec.command else ""
        behavior = self._behavior(script)
        with self._lock:
            self.runs.append(spec)
            self.script_order.append(script)
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
            self.running[spec.name] = spec
        try:
            for line in behavior.lines:
                sink(line)
            return self._wait(spec, behavior, timeout, cancel)
        finally:
            with self._lock:
                self.concurrent -= 1
                self.running.pop(spec.name, None)

    def run_container_binary(
        self, spec: RunSpec, sink: Callable[[bytes], None], *, timeout: float | None = None
    ) -> ExecResult:
        """Run a binary container command with scripted output and exit status."""
        script = spec.command[-1] if spec.command else ""
        behavior = self._behavior(script)
        with self._lock:
            self.runs.append(spec)
            self.script_order.append(script)
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
            self.running[spec.name] = spec
        try:
            for chunk in behavior.binary_chunks:
                sink(chunk)
            return self._wait(spec, behavior, timeout, None)
        finally:
            with self._lock:
                self.concurrent -= 1
                self.running.pop(spec.name, None)

    def _wait(
        self, spec: RunSpec, behavior: Behavior, timeout: float | None, cancel: CancelSignal | None
    ) -> ExecResult:
        started = time.monotonic()
        while behavior.block or time.monotonic() - started < behavior.delay:
            if spec.name in self.killed:
                return ExecResult(137)
            if cancel is not None and cancel.is_set():
                return ExecResult(137, cancelled=True)
            if timeout is not None and time.monotonic() - started >= timeout:
                return ExecResult(137, timed_out=True)
            time.sleep(POLL)
        with self._lock:
            code = behavior.sequence.pop(0) if behavior.sequence else behavior.exit_code
        return ExecResult(code)

    def kill_by_label(self, label: str) -> int:
        """Kill the running fake containers that carry ``label``."""
        key, _, value = label.partition("=")
        with self._lock:
            self.kills.append(label)
            hit = [n for n, s in self.running.items() if s.labels.get(key) == value]
            self.killed.update(hit)
            return len(hit)

    def remove_image(self, image: str) -> None:
        """Record the removal."""
        self.removed.append(image)
        self.images.discard(image)


class FakeSource:
    """Writes the given files into the workspace instead of cloning."""

    def __init__(self, files: Mapping[str, str] | None = None, error: str | None = None) -> None:
        self.files = dict(files or {"README.md": "hello\n", "Dockerfile": "FROM scratch\n"})
        self.error = error
        self.requests: list[CheckoutRequest] = []
        self.reads: list[str] = []

    def checkout(self, request: CheckoutRequest, destination: Path) -> None:
        """Populate ``destination``."""
        self.requests.append(request)
        if self.error:
            raise AdapterError(self.error)
        for name, content in self.files.items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)

    def read_file(self, request: CheckoutRequest, path: str) -> str | None:
        """The file of the fake tree (``error`` only affects the checkout), or ``None``."""
        self.reads.append(path)
        return self.files.get(path)


class FakeDeployer:
    """Records deploy requests; optionally fails."""

    def __init__(self, error: str | None = None) -> None:
        self.requests: list[DeployRequest] = []
        self.error = error

    def deploy(self, request: DeployRequest, sink: LineSink, cancel: CancelSignal) -> None:
        """Record the request."""
        self.requests.append(request)
        sink(f"deploying {len(request.services)} service(s)")
        if self.error:
            raise DeployError(self.error)


class FakeProbe:
    """Returns scripted statuses (or raises) for health checks."""

    def __init__(self, results: list[int | None]) -> None:
        self.results = list(results)
        self.calls: list[str] = []

    def get_status(self, url: str, timeout: float) -> int:
        """Pop the next scripted result; the last one repeats."""
        self.calls.append(url)
        value = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if value is None:
            raise AdapterError("connection refused")
        return value


@dataclass(slots=True)
class Harness:
    """A fully wired execution environment on a test database with fakes."""

    env: ExecutionEnv
    runtime: FakeContainerRuntime
    source: FakeSource
    deployer: FakeDeployer
    probe: FakeProbe
    cipher: SecretCipher
    project: str
    tmp_path: Path
    engine: Engine
    secrets_registered: list[str] = field(default_factory=list)

    def uow(self) -> UnitOfWork:
        """A fresh unit of work."""
        return self.env.uow()

    def add_secret(
        self, name: str, value: str, environment: EnvironmentName = EnvironmentName.DEVELOPMENT
    ) -> None:
        """Store an encrypted secret."""
        service = SecretService(self.uow(), self.cipher)
        service.set(self.project, environment, name, value, Actor.system("test"))

    def queue(
        self,
        jenkinsfile: str,
        *,
        branch: str = "development",
        environment: EnvironmentName = EnvironmentName.DEVELOPMENT,
        commit_sha: str = SHA,
    ) -> Run:
        """Queue a run through ``RunService``."""
        return RunService(self.uow()).create_queued(
            self.project,
            environment,
            trigger=RunTrigger.MANUAL,
            branch=branch,
            commit_sha=commit_sha,
            pipeline_source=PipelineSource.PLATFORM,
            pipeline_text=jenkinsfile,
        )

    def execute(self, run: Run, token: CancelToken | None = None) -> RunOutcome:
        """Claim and execute ``run`` synchronously (it must be the oldest queued)."""
        claimed = self.env.lifecycle().claim_next()
        assert claimed is not None
        assert claimed.id == run.id
        return RunExecutor(self.env).execute(run.id, token or CancelToken())

    def run(self, jenkinsfile: str, **kwargs: object) -> tuple[Run, RunOutcome]:
        """Queue and execute a pipeline."""
        run = self.queue(jenkinsfile, **kwargs)  # type: ignore[arg-type]
        return run, self.execute(run)

    def detail(self, run: Run) -> RunDetail:
        """The run read model."""
        return RunQueryService(self.uow()).detail(run.id)

    def stage_statuses(self, run: Run) -> dict[str, RunStatus]:
        """Stage name to status."""
        return {d.stage.name: d.stage.status for d in self.detail(run).stages}

    def steps(self, run: Run, stage: str) -> list[tuple[str, RunStatus]]:
        """``(label, status)`` of the steps of a stage."""
        for item in self.detail(run).stages:
            if item.stage.name == stage:
                return [(s.label, s.status) for s in item.steps]
        raise KeyError(stage)

    def log_of(self, run: Run, stage: str, index: int = 0) -> str:
        """The full log text of a step."""
        for item in self.detail(run).stages:
            if item.stage.name == stage:
                step = item.steps[index]
                path = self.env.logs.path(run.id, step.id)
                return path.read_text() if path.exists() else ""
        raise KeyError(stage)

    def all_logs(self, run: Run) -> str:
        """The concatenated text of every step log."""
        return "\n".join(
            self.env.logs.path(run.id, step.id).read_text()
            for item in self.detail(run).stages
            for step in item.steps
            if self.env.logs.path(run.id, step.id).exists()
        )


def make_harness(
    tmp_path: Path,
    engine: Engine,
    cipher: SecretCipher,
    project: str,
    config: ExecutionConfig | None = None,
    source: FakeSource | None = None,
) -> Harness:
    """Wire fakes onto the test database."""
    runtime = FakeContainerRuntime()
    fake_source = source or FakeSource()
    deployer = FakeDeployer()
    probe = FakeProbe([200])
    session_factory = create_session_factory(engine)
    data = tmp_path / "data"
    secret_factory: Callable[[], SecretService] = lambda: SecretService(  # noqa: E731
        UnitOfWork(session_factory), cipher
    )
    env = ExecutionEnv(
        config=config or ExecutionConfig(health_interval=0.01, cancel_poll_interval=0.05),
        runtime=runtime,
        source=fake_source,
        deployer=deployer,
        probe=probe,
        workspaces=WorkspaceManager(data / "workspaces"),
        logs=LogStore(data / "logs"),
        session_factory=session_factory,
        secret_service=secret_factory,
    )
    return Harness(env, runtime, fake_source, deployer, probe, cipher, project, tmp_path, engine)
