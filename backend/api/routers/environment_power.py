"""Environment start and stop endpoints."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field

from backend.api.deps import Container, require
from backend.api.principal import Principal
from backend.containers.wire import EnvironmentPowerView
from backend.domain.access import Action
from backend.domain.enums import EnvironmentName
from backend.domain.errors import ValidationError

router = APIRouter(prefix="/api/projects", tags=["environments"])
Reader = Annotated[Principal, Depends(require(Action.ENVIRONMENT_POWER_READ))]
Starter = Annotated[Principal, Depends(require(Action.ENVIRONMENT_START))]
Stopper = Annotated[Principal, Depends(require(Action.ENVIRONMENT_STOP))]


@dataclass(frozen=True, slots=True)
class EnvPath:
    slug: str
    env: EnvironmentName


class StopRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm: str | None = Field(default=None, max_length=64)


@router.get("/{slug}/environments/{env}/power")
def read_power(
    path: Annotated[EnvPath, Depends()], principal: Reader, container: Container
) -> EnvironmentPowerView:
    return container.container_gateway().environment_power(path.slug, path.env, principal.actor)


@router.post("/{slug}/environments/{env}/power/stop")
def stop_power(
    path: Annotated[EnvPath, Depends()],
    principal: Stopper,
    container: Container,
    body: StopRequest | None = None,
) -> EnvironmentPowerView:
    body = body or StopRequest()
    if path.env is EnvironmentName.PRODUCTION and body.confirm != path.slug:
        raise ValidationError(f"Type the project slug '{path.slug}' to stop production.")
    return container.container_gateway().stop_environment(path.slug, path.env, principal.actor)


@router.post("/{slug}/environments/{env}/power/start")
def start_power(
    path: Annotated[EnvPath, Depends()], principal: Starter, container: Container
) -> EnvironmentPowerView:
    return container.container_gateway().start_environment(path.slug, path.env, principal.actor)
