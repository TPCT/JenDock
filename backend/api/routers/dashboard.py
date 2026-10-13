"""The home dashboard: one aggregated read for the landing page."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends

from backend.api import authz
from backend.api.deps import Container, Target, require
from backend.api.principal import Principal
from backend.api.schemas.dashboard import DashboardResponse
from backend.containers.wire import HostSampleView
from backend.domain.access import Action
from backend.domain.errors import DomainError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])
Reader = Annotated[Principal, Depends(require(Action.DASHBOARD_READ, Target.NONE))]
HOST_UNAVAILABLE = "Host telemetry is not available right now."


@router.get("")
def dashboard(principal: Reader, container: Container) -> DashboardResponse:
    """Recent deployments, runs needing attention, auto-deploy hints, expiring certificates and
    a host summary. The database cards cost a fixed number of queries; a worker that cannot be
    reached only empties the host card (``host_error``), never the page."""
    scope = authz.dashboard_scope(principal)
    data = container.dashboard_service().load(scope)
    host: HostSampleView | None = None
    error: str | None = None
    if scope.host:  # a member without any grant has no business with the host's load
        try:
            host = container.container_gateway().host_telemetry(principal.actor).latest
        except DomainError:
            logger.warning("dashboard host telemetry unavailable", exc_info=True)
            error = HOST_UNAVAILABLE
    return DashboardResponse.build(data, host, error)
