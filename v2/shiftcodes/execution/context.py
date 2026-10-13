"""Per-run and per-step execution contexts."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from shiftcodes.adapters.container_runtime import CancelSignal, ContainerRuntime
from shiftcodes.adapters.deployer import Deployer
from shiftcodes.adapters.http_probe import HttpProbe
from shiftcodes.adapters.sources import SourceProvider
from shiftcodes.adapters.workspace import WorkspaceManager
from shiftcodes.domain.enums import GitTransport, PipelineSource
from shiftcodes.execution.cancel import CancelToken, NeverCancelled
from shiftcodes.execution.config import ExecutionConfig
from shiftcodes.execution.errors import DeadlineExceeded, RunCancelled
from shiftcodes.execution.logs import RunRedactor, StepLogWriter
from shiftcodes.execution.scope import Scope
from shiftcodes.pipeline.ast import StepCall, StringValue
from shiftcodes.services.run_progress import RunRecorder

NEVER = NeverCancelled()


@dataclass(frozen=True, slots=True)
class RunJob:
    """What the worker knows about a claimed run."""

    run_id: int
    number: int
    project: str
    environment: str
    environment_id: int
    branch: str
    commit_sha: str
    repository: str
    compose_project: str | None
    compose_files: tuple[str, ...]
    services: tuple[str, ...]
    pipeline_text: str
    git_transport: GitTransport = GitTransport.HTTPS
    pipeline_source: PipelineSource = PipelineSource.PLATFORM


class ImageBook:
    """Thread-safe record of what one run built, pulled and looked up."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._built: dict[str, str] = {}
        self._pulled: set[str] = set()
        self._networks: dict[str, bool] = {}

    def built(self, logical: str, tag: str) -> None:
        """Remember that logical image ``logical`` is ``tag``."""
        with self._lock:
            self._built[logical] = tag

    def tag_of(self, logical: str) -> str | None:
        """The tag built for ``logical``, if any."""
        with self._lock:
            return self._built.get(logical)

    def tags(self) -> list[str]:
        """Every tag built in this run."""
        with self._lock:
            return list(self._built.values())

    def ensure(self, key: str, check: Callable[[], bool]) -> bool:
        """Memoize ``check`` (an expensive docker query) per ``key``."""
        with self._lock:
            if key in self._networks:
                return self._networks[key]
        value = check()
        with self._lock:
            self._networks[key] = value
        return value

    def mark_pulled(self, image: str) -> bool:
        """Record a pull; return False when the image was already handled in this run."""
        with self._lock:
            if image in self._pulled:
                return False
            self._pulled.add(image)
            return True


class StepSequencer(Protocol):
    """Runs nested steps; implemented by the step executor."""

    def run_all(self, steps: Sequence[StepCall], scope: Scope, stage_id: int) -> None:
        """Execute ``steps`` in order."""


@dataclass(slots=True)
class RunContext:
    """Collaborators and state shared by everything that executes one run."""

    job: RunJob
    config: ExecutionConfig
    runtime: ContainerRuntime
    deployer: Deployer
    probe: HttpProbe
    source: SourceProvider
    recorder: RunRecorder
    redactor: RunRedactor
    cancel: CancelToken
    workspaces: WorkspaceManager
    workspace: Path
    resolve_secrets: Callable[[Sequence[str]], Mapping[str, str]]
    images: ImageBook


@dataclass(frozen=True, slots=True)
class StepSite:
    """Where a step runs: its stage and its own step record."""

    stage_id: int
    step_id: int


class StepContext:
    """What a step sees while it executes."""

    def __init__(
        self, run: RunContext, scope: Scope, site: StepSite, log: StepLogWriter, seq: StepSequencer
    ) -> None:
        self.run = run
        self.scope = scope
        self.site = site
        self.exit_code: int | None = None
        self._log = log
        self._sequencer = seq

    def line(self, text: str) -> None:
        """Write a line to the step log (masked)."""
        self._log.write_line(text)

    def text(self, value: StringValue, *, for_shell: bool = False) -> str:
        """Interpolate ``value`` in the current scope."""
        return self.scope.render(value, for_shell=for_shell)

    @property
    def cancel_signal(self) -> CancelSignal:
        """The signal containers and builds watch (post steps are not cancellable)."""
        return NEVER if self.scope.post else self.run.cancel

    def checkpoint(self) -> None:
        """Raise when the run was cancelled or the deadline passed."""
        if not self.scope.post and self.run.cancel.is_set():
            raise RunCancelled
        remaining = self.scope.remaining()
        if remaining is not None and remaining <= 0:
            raise DeadlineExceeded("timeout exceeded")

    def effective_timeout(self) -> float:
        """Seconds a container may run: the nearest deadline, or the default step limit."""
        remaining = self.scope.remaining()
        default = self.run.config.step_timeout_seconds
        return default if remaining is None else min(default, remaining)

    def run_body(self, steps: Sequence[StepCall], scope: Scope) -> None:
        """Execute nested steps in ``scope`` as part of the same stage."""
        self._sequencer.run_all(steps, scope, self.site.stage_id)
