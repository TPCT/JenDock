"""Reading the state of containers and images through ``docker inspect``."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from backend.adapters.docker_commands import CommandRunner
from backend.adapters.errors import ContainerRuntimeError


@dataclass(frozen=True, slots=True)
class ContainerState:
    """What the deployer needs to know about one container."""

    id: str
    image_ref: str
    image_id: str
    status: str
    health: str | None
    restart_count: int
    exit_code: int

    @property
    def running(self) -> bool:
        """Whether the container is up (a restarting one is not)."""
        return self.status == "running"


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _state_of(raw: object) -> ContainerState:
    if not isinstance(raw, dict):
        raise ContainerRuntimeError("unexpected docker inspect output")
    state = raw.get("State")
    config = raw.get("Config")
    state = state if isinstance(state, dict) else {}
    config = config if isinstance(config, dict) else {}
    health = state.get("Health")
    health_status = _text(health.get("Status")) if isinstance(health, dict) else ""
    restarts = raw.get("RestartCount")
    exit_code = state.get("ExitCode")
    return ContainerState(
        id=_text(raw.get("Id")),
        image_ref=_text(config.get("Image")),
        image_id=_text(raw.get("Image")),
        status=_text(state.get("Status")),
        health=health_status or None,
        restart_count=restarts if isinstance(restarts, int) else 0,
        exit_code=exit_code if isinstance(exit_code, int) else 0,
    )


class ContainerInspector:
    """Inspect containers and images."""

    def __init__(self, docker: CommandRunner) -> None:
        self._docker = docker

    def containers(self, ids: Sequence[str]) -> list[ContainerState]:
        """The state of the containers with ``ids`` (a container that vanished is omitted)."""
        if not ids:
            return []
        result = self._docker.capture(["inspect", "--type", "container", *ids])
        if result.returncode != 0 and not result.stdout.strip().startswith("["):
            raise ContainerRuntimeError(f"cannot inspect containers: {result.stderr.strip()}")
        try:
            parsed = json.loads(result.stdout or "[]")
        except json.JSONDecodeError as error:
            raise ContainerRuntimeError("docker inspect returned invalid JSON") from error
        return [_state_of(item) for item in parsed] if isinstance(parsed, list) else []

    def image_id(self, reference: str) -> str | None:
        """The full image id behind a tag or id, or ``None`` when the image is absent."""
        result = self._docker.capture(["image", "inspect", "--format", "{{.Id}}", reference])
        return result.stdout.strip() or None if result.returncode == 0 else None

    def all_container_image_ids(self) -> frozenset[str]:
        """Ids of the images used by any container, running or stopped."""
        listing = self._docker.capture(["ps", "--all", "--quiet", "--no-trunc"])
        if listing.returncode != 0:
            raise ContainerRuntimeError(f"cannot list containers: {listing.stderr.strip()}")
        return frozenset(c.image_id for c in self.containers(listing.stdout.split()))
