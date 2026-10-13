"""CPU and memory limits per compose service of an environment.

The worker owns Docker: it validates against the host's capacity, saves the limits (so they
survive every deploy), renders the managed compose override and, on request, runs
``docker update`` on the running containers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from backend.api import authz
from backend.api.deps import Container, require
from backend.api.principal import Principal
from backend.api.schemas.containers import ResourceLimitsRequest
from backend.containers.wire import (
    ResourceChangeView,
    ResourceOverviewView,
    ResourceUsageView,
)
from backend.domain.access import Action
from backend.domain.enums import EnvironmentName
from backend.domain.resources import ResourceLimits, validate_service_name

router = APIRouter(prefix="/api/projects/{slug}/environments/{env}/resources", tags=["resources"])
Reader = Annotated[Principal, Depends(require(Action.CONTAINER_READ))]
Editor = Annotated[Principal, Depends(require(Action.CONTAINER_LIMITS))]


@dataclass(frozen=True, slots=True)
class EnvPath:
    """Path parameters identifying an environment."""

    slug: str
    env: EnvironmentName


@dataclass(frozen=True, slots=True)
class ServicePath:
    """Path parameters identifying one compose service of an environment."""

    slug: str
    env: EnvironmentName
    service: str

    def __post_init__(self) -> None:
        validate_service_name(self.service)


EnvironmentPath = Annotated[EnvPath, Depends()]
ServiceTarget = Annotated[ServicePath, Depends()]


@router.get("")
def list_resources(
    path: EnvironmentPath, principal: Reader, container: Container
) -> ResourceOverviewView:
    """The host's CPUs and memory and the saved limits of every service."""
    overview = container.container_gateway().resources_list(path.slug, path.env, principal.actor)
    names = [item.service for item in overview.services]
    allowed = set(authz.visible_services(principal, path.slug, path.env, names))
    return overview.model_copy(
        update={"services": tuple(item for item in overview.services if item.service in allowed)}
    )


@router.get("/stats")
def resource_usage(
    path: EnvironmentPath, principal: Reader, container: Container
) -> ResourceUsageView:
    """Live CPU and memory use of the running containers (``docker stats --no-stream``) with
    the host's CPUs and memory."""
    usage = container.container_gateway().resources_usage(path.slug, path.env, principal.actor)
    names = [item.service or "" for item in usage.containers]
    allowed = set(authz.visible_services(principal, path.slug, path.env, names))
    return usage.model_copy(
        update={
            "containers": tuple(
                item for item in usage.containers if (item.service or "") in allowed
            )
        }
    )


@router.put("/{service}")
def save_resources(
    path: ServiceTarget, body: ResourceLimitsRequest, principal: Editor, container: Container
) -> ResourceChangeView:
    """Save the complete set of limits of a service; with ``apply_now`` also update its running
    containers without a restart."""
    limits = ResourceLimits(
        cpus=body.cpus,
        memory_bytes=body.memory_bytes,
        memory_reservation_bytes=body.memory_reservation_bytes,
        cpu_shares=body.cpu_shares,
    )
    gateway = container.container_gateway()
    return gateway.resources_save(
        path.slug, path.env, path.service, limits, body.apply_now, principal.actor
    )


@router.delete("/{service}")
def remove_resources(
    path: ServiceTarget,
    principal: Editor,
    container: Container,
    apply_now: Annotated[bool, Query()] = False,
) -> ResourceChangeView:
    """Remove the saved limits of a service (the next deploy runs it without them)."""
    gateway = container.container_gateway()
    return gateway.resources_remove(path.slug, path.env, path.service, apply_now, principal.actor)


@router.post("/{service}/apply")
def apply_resources(
    path: ServiceTarget, principal: Editor, container: Container
) -> ResourceChangeView:
    """Update the running containers of a service with its saved limits (no restart)."""
    gateway = container.container_gateway()
    return gateway.resources_apply(path.slug, path.env, path.service, principal.actor)
