"""The container runtime port: everything the pipeline runner needs from Docker."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

LineSink = Callable[[str], None]
DEFAULT_CPU_SHARES = 512


class CancelSignal(Protocol):
    """Something that can be asked whether the work should stop."""

    def is_set(self) -> bool:
        """Whether cancellation was requested."""


@dataclass(frozen=True, slots=True)
class Mount:
    """A bind mount of a host path into a container."""

    source: Path
    target: str
    read_only: bool = False


@dataclass(frozen=True, slots=True)
class BuildSpec:
    """An image build. Build argument values may be secret: they never reach argv."""

    context: Path
    dockerfile: Path
    tag: str
    target: str | None = None
    build_args: Mapping[str, str] = field(default_factory=dict)
    labels: Mapping[str, str] = field(default_factory=dict)
    memory: str | None = None


@dataclass(frozen=True, slots=True)
class RunSpec:
    """A container to run. ``env`` values (secrets included) travel via an env file."""

    image: str
    name: str
    command: tuple[str, ...] = ()
    entrypoint: str | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    workdir: str | None = None
    mounts: tuple[Mount, ...] = ()
    network: str | None = None
    labels: Mapping[str, str] = field(default_factory=dict)
    cpus: float | None = None
    memory: str | None = None
    cpu_shares: int = DEFAULT_CPU_SHARES
    user: str | None = None


@dataclass(frozen=True, slots=True)
class ExecResult:
    """How a build or container ended."""

    exit_code: int
    timed_out: bool = False
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        """Whether it finished by itself with exit code 0."""
        return self.exit_code == 0 and not self.timed_out and not self.cancelled


class ContainerRuntime(Protocol):
    """Docker as the pipeline runner sees it. Output is streamed line by line to a sink."""

    def build_image(
        self,
        spec: BuildSpec,
        sink: LineSink,
        *,
        timeout: float | None = None,
        cancel: CancelSignal | None = None,
    ) -> ExecResult:
        """Build an image, streaming the build output."""

    def pull_image(
        self,
        image: str,
        sink: LineSink,
        *,
        timeout: float | None = None,
        cancel: CancelSignal | None = None,
    ) -> ExecResult:
        """Pull an image, streaming progress."""

    def image_exists(self, image: str) -> bool:
        """Whether the image is present locally."""

    def network_exists(self, name: str) -> bool:
        """Whether a Docker network called ``name`` exists."""

    def run_container(
        self,
        spec: RunSpec,
        sink: LineSink,
        *,
        timeout: float | None = None,
        cancel: CancelSignal | None = None,
    ) -> ExecResult:
        """Run a container to completion, streaming combined stdout and stderr."""

    def kill_by_label(self, label: str) -> int:
        """Kill running containers carrying ``label`` (``key=value``); return how many."""

    def remove_image(self, image: str) -> None:
        """Remove an image tag; a missing image is not an error."""
