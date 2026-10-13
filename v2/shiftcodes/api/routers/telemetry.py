"""Host telemetry endpoint."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from shiftcodes.api.deps import Container, require_scope
from shiftcodes.api.principal import Principal
from shiftcodes.containers.wire import HostTelemetryView
from shiftcodes.domain.enums import TokenScope

router = APIRouter(prefix="/api/telemetry", tags=["telemetry"])
Reader = Annotated[Principal, Depends(require_scope(TokenScope.READ))]


@router.get("/host")
def host_telemetry(principal: Reader, container: Container) -> HostTelemetryView:
    """CPU, RAM and disk of the host as the worker sees them, with a short history."""
    return container.container_gateway().host_telemetry(principal.actor)
