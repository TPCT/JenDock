"""Container management endpoints. Every Docker operation is done by the worker."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from shiftcodes.api.container_logs import ContainerLogStream
from shiftcodes.api.deps import Container, require_scope
from shiftcodes.api.principal import Principal
from shiftcodes.api.schemas.containers import (
    ActionRequest,
    ConfirmRequest,
    EnvUpdateRequest,
    LimitsRequest,
)
from shiftcodes.containers.files import DEFAULT_READ_BYTES, MAX_READ_BYTES
from shiftcodes.containers.logs import DEFAULT_TAIL, MAX_TAIL, LogCursor
from shiftcodes.containers.wire import (
    ActionPlanView,
    ActionResultView,
    ContainerStatsView,
    ContainerView,
    DirectoryView,
    EnvRevealView,
    EnvUpdateView,
    EnvVarView,
    FileView,
    LogPageView,
)
from shiftcodes.domain.containers import validate_container_id, validate_container_path
from shiftcodes.domain.enums import EnvironmentName, TokenScope

router = APIRouter(prefix="/api/projects/{slug}/environments/{env}/containers", tags=["containers"])
Reader = Annotated[Principal, Depends(require_scope(TokenScope.READ))]
Manager = Annotated[Principal, Depends(require_scope(TokenScope.CONTAINERS))]
BackgroundManager = Annotated[
    Principal, Depends(require_scope(TokenScope.CONTAINERS, background=True))
]
Admin = Annotated[Principal, Depends(require_scope(TokenScope.ADMIN))]


@dataclass(frozen=True, slots=True)
class EnvPath:
    """Path parameters identifying an environment."""

    slug: str
    env: EnvironmentName


@dataclass(frozen=True, slots=True)
class ContainerPath:
    """Path parameters identifying one container by its full 64 character id."""

    slug: str
    env: EnvironmentName
    container_id: str

    def __post_init__(self) -> None:
        validate_container_id(self.container_id)


@dataclass(frozen=True, slots=True)
class EnvVarPath:
    """Path parameters identifying one variable of one container."""

    slug: str
    env: EnvironmentName
    container_id: str
    name: str

    def __post_init__(self) -> None:
        validate_container_id(self.container_id)


EnvironmentPath = Annotated[EnvPath, Depends()]
Target = Annotated[ContainerPath, Depends()]
VarTarget = Annotated[EnvVarPath, Depends()]


@router.get("")
def list_containers(
    path: EnvironmentPath, principal: Reader, container: Container
) -> list[ContainerView]:
    """The containers of the environment's compose project (no other container is visible)."""
    gateway = container.container_gateway()
    return list(gateway.list_containers(path.slug, path.env, principal.actor))


@router.get("/{container_id}")
def get_container(path: Target, principal: Reader, container: Container) -> ContainerView:
    """One container by its exact id."""
    gateway = container.container_gateway()
    return gateway.get_container(path.slug, path.env, path.container_id, principal.actor)


@router.post("/{container_id}/actions")
def plan_action(
    path: Target, body: ActionRequest, principal: Manager, container: Container
) -> ActionPlanView:
    """Step one of start/stop/restart: what would happen and a single-use confirmation."""
    gateway = container.container_gateway()
    return gateway.plan_action(path.slug, path.env, path.container_id, body.action, principal.actor)


@router.post("/{container_id}/actions/confirm")
def confirm_action(
    path: Target, body: ConfirmRequest, principal: Manager, container: Container
) -> ActionResultView:
    """Step two: execute the planned action on exactly that container id."""
    gateway = container.container_gateway()
    return gateway.confirm_action(
        path.slug, path.env, path.container_id, body.confirmation, principal.actor
    )


@router.get("/{container_id}/logs")
def tail_logs(  # noqa: PLR0913, PLR0917 - path, principal, container and three query values
    path: Target,
    principal: Manager,
    container: Container,
    tail: Annotated[int, Query(ge=1, le=MAX_TAIL)] = DEFAULT_TAIL,
    since: Annotated[str | None, Query(max_length=40)] = None,
    skip: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> LogPageView:
    """The last ``tail`` lines (masked), or the lines after the cursor ``since``/``skip``."""
    cursor = LogCursor(since, skip) if since else None
    gateway = container.container_gateway()
    return gateway.logs(
        path.slug, path.env, path.container_id, principal.actor, tail=tail, cursor=cursor
    )


@router.get("/{container_id}/logs/stream")
def follow_logs(
    path: Target,
    principal: BackgroundManager,
    container: Container,
    tail: Annotated[int, Query(ge=1, le=MAX_TAIL)] = 100,
) -> StreamingResponse:
    """Server-Sent Events: ``log`` events with masked lines, heartbeats, ``error``, ``end``."""
    gateway = container.container_gateway()
    gateway.get_container(path.slug, path.env, path.container_id, principal.actor)  # 404 first
    stream = ContainerLogStream(container, principal, path.slug, path.env, path.container_id, tail)
    return StreamingResponse(
        iter(stream),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


@router.get("/{container_id}/env")
def list_env(path: Target, principal: Reader, container: Container) -> list[EnvVarView]:
    """Variable names with masked values."""
    gateway = container.container_gateway()
    return list(gateway.env_list(path.slug, path.env, path.container_id, principal.actor))


@router.post("/{container_id}/env/{name}/reveal")
def reveal_env(path: VarTarget, principal: Admin, container: Container) -> EnvRevealView:
    """The plaintext of one variable; the worker audits the reveal."""
    gateway = container.container_gateway()
    return gateway.env_reveal(path.slug, path.env, path.container_id, path.name, principal.actor)


@router.put("/{container_id}/env")
def update_env(
    path: Target, body: EnvUpdateRequest, principal: Manager, container: Container
) -> EnvUpdateView:
    """Store overrides for the container's service and recreate only that service."""
    gateway = container.container_gateway()
    return gateway.env_update(
        path.slug, path.env, path.container_id, body.values, body.remove, principal.actor
    )


@router.put("/{container_id}/limits")
def update_limits(
    path: Target, body: LimitsRequest, principal: Manager, container: Container
) -> ContainerView:
    """Set CPU and/or memory limits (within the configured maxima); persisted as an override."""
    gateway = container.container_gateway()
    return gateway.update_limits(
        path.slug, path.env, path.container_id, body.cpus, body.memory, principal.actor
    )


@router.get("/{container_id}/files")
def list_files(
    path: Target,
    principal: Manager,
    container: Container,
    directory: Annotated[str, Query(alias="path", max_length=1024)] = "/",
) -> DirectoryView:
    """The entries of a directory of the running container (read-only)."""
    gateway = container.container_gateway()
    clean = validate_container_path(directory)
    return gateway.list_files(path.slug, path.env, path.container_id, clean, principal.actor)


@router.get("/{container_id}/files/content")
def read_file(
    path: Target,
    principal: Manager,
    container: Container,
    file_path: Annotated[str, Query(alias="path", max_length=1024)],
    max_bytes: Annotated[int, Query(ge=1, le=MAX_READ_BYTES)] = DEFAULT_READ_BYTES,
) -> FileView:
    """A text file, cut at ``max_bytes``; binary files report ``binary`` and no text."""
    gateway = container.container_gateway()
    clean = validate_container_path(file_path)
    return gateway.read_file(
        path.slug, path.env, path.container_id, clean, max_bytes, principal.actor
    )


@router.get("/{container_id}/stats")
def container_stats(path: Target, principal: Reader, container: Container) -> ContainerStatsView:
    """Latest ``docker stats`` sample and the in-memory history of the container."""
    gateway = container.container_gateway()
    return gateway.container_stats(path.slug, path.env, path.container_id, principal.actor)
