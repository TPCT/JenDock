"""The deployment port. The Compose implementation is ``compose_deployer.ComposeDeployer``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from shiftcodes.adapters.container_runtime import CancelSignal, LineSink
from shiftcodes.adapters.errors import DeployError


@dataclass(frozen=True, slots=True)
class ServiceImage:
    """One service and the built image it must run."""

    service: str
    image: str


@dataclass(frozen=True, slots=True)
class HttpCheck:
    """An HTTP endpoint that must answer with ``expect_status`` within ``timeout`` seconds."""

    url: str
    expect_status: int = 200
    timeout: int = 60


@dataclass(frozen=True, slots=True)
class DeployRequest:
    """Everything a deployment needs; the images are already built tags."""

    project: str
    environment: str
    compose_project: str | None
    compose_files: tuple[str, ...]
    services: tuple[ServiceImage, ...]
    run_id: int
    commit_sha: str
    http_check: HttpCheck | None = None
    compose_env_files: tuple[str, ...] = ()


class Deployer(Protocol):
    """Rolls services of an environment to new images."""

    def deploy(self, request: DeployRequest, sink: LineSink, cancel: CancelSignal) -> None:
        """Deploy ``request``; raise ``DeployError`` when it fails."""


@runtime_checkable
class Recoverable(Protocol):
    """A deployer that can finish or undo deployments a dead worker left behind."""

    def recover(self) -> list[int]:
        """Roll back interrupted deployments; return the deployment ids handled."""


class UnavailableDeployer:
    """Fails every deployment; stands in where no real deployer is wired."""

    def deploy(self, request: DeployRequest, sink: LineSink, cancel: CancelSignal) -> None:  # noqa: ARG002
        """Always fails."""
        raise DeployError("deploy is not available")
