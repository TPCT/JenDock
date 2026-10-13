"""The deployment port. The real Compose deployer arrives with Task 7."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from shiftcodes.adapters.container_runtime import CancelSignal, LineSink
from shiftcodes.adapters.errors import DeployError


@dataclass(frozen=True, slots=True)
class ServiceImage:
    """One service and the built image it must run."""

    service: str
    image: str


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


class Deployer(Protocol):
    """Rolls services of an environment to new images."""

    def deploy(self, request: DeployRequest, sink: LineSink, cancel: CancelSignal) -> None:
        """Deploy ``request``; raise ``DeployError`` when it fails."""


class UnavailableDeployer:
    """Fails every deployment until Task 7 provides the Compose deployer."""

    def deploy(self, request: DeployRequest, sink: LineSink, cancel: CancelSignal) -> None:  # noqa: ARG002
        """Always fails."""
        raise DeployError("deploy is not available yet (Task 7)")
