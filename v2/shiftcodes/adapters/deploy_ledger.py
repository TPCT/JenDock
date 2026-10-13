"""The deployment record port: what the deployer reports about each rollout."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from shiftcodes.domain.enums import DeploymentStatus

ServiceJson = dict[str, str | None]


@dataclass(frozen=True, slots=True)
class ServiceChange:
    """One service rolled to a new image. ``previous_*`` are ``None`` for a service that was
    not running before the deployment."""

    service: str
    new_image: str
    new_image_id: str | None = None
    previous_image: str | None = None
    previous_image_id: str | None = None

    def to_json(self) -> ServiceJson:
        """The form stored in the deployment record and the journal."""
        return {
            "service": self.service,
            "previous_image": self.previous_image,
            "previous_image_id": self.previous_image_id,
            "new_image": self.new_image,
            "new_image_id": self.new_image_id,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, str | None]) -> ServiceChange:
        """Parse the stored form."""
        return cls(
            service=data.get("service") or "",
            new_image=data.get("new_image") or "",
            new_image_id=data.get("new_image_id"),
            previous_image=data.get("previous_image"),
            previous_image_id=data.get("previous_image_id"),
        )


class DeploymentLedger(Protocol):
    """Persists deployments; implemented over the database in ``services.deployments``."""

    def begin(self, project: str, environment: str, run_id: int, commit_sha: str) -> int:
        """Record a deployment as ``deploying`` (adopting a pending redeploy of the run)."""

    def record_services(self, deployment_id: int, changes: Sequence[ServiceChange]) -> None:
        """Store the previous and new image of every service."""

    def finish(self, deployment_id: int, status: DeploymentStatus, message: str | None) -> None:
        """Record the final status."""

    def fail_unfinished(self, message: str) -> list[int]:
        """Mark every deployment still ``deploying`` as failed; return their ids."""

    def protected_images(self, project: str, environment: str) -> frozenset[str]:
        """Image references and ids that image retention must keep for the environment."""
