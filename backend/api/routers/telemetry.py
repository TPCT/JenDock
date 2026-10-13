"""Host telemetry endpoint."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from backend.api.deps import Container, Target, require
from backend.api.principal import Principal
from backend.containers.wire import HostTelemetryView
from backend.domain.access import Action

router = APIRouter(prefix="/api/telemetry", tags=["telemetry"])
Reader = Annotated[Principal, Depends(require(Action.TELEMETRY_READ, Target.NONE))]


@router.get("/host")
def host_telemetry(principal: Reader, container: Container) -> HostTelemetryView:
    """CPU, RAM and disk of the host as the worker sees them, with a short history."""
    return container.container_gateway().host_telemetry(principal.actor)
