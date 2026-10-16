"""Periodic stop of idle development environments."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol

from backend.containers.power import EnvironmentPowerService, PowerState
from backend.domain.actor import Actor
from backend.domain.clock import Clock, utcnow
from backend.domain.enums import EnvironmentName
from backend.repositories.database import SessionFactory, UnitOfWork

logger = logging.getLogger(__name__)


class StopSignal(Protocol):
    def wait(self, timeout: float) -> bool: ...


class AutoStopScheduler:
    """One tick scans configured development environments and stops those past their deadline."""

    def __init__(
        self,
        session_factory: SessionFactory,
        power: EnvironmentPowerService,
        interval: float = 300,
        clock: Clock = utcnow,
    ) -> None:
        self._sessions = session_factory
        self._power = power
        self._interval = interval
        self._clock = clock

    def tick(self, now: datetime | None = None) -> None:
        """Check every configured environment once; isolated failures do not stop the scan."""
        current = now or self._clock()
        uow = UnitOfWork(self._sessions)
        with uow:
            candidates = list(uow.environments.list_auto_stop_candidates())
        for slug, environment in candidates:
            if environment is not EnvironmentName.DEVELOPMENT:
                continue
            try:
                state = self._power.state(slug, environment)
                if state.state is not PowerState.RUNNING or state.auto_stop_due_at is None:
                    continue
                if current < state.auto_stop_due_at:
                    continue
                reference = max(
                    item for item in (state.started_at, state.last_deployed_at) if item is not None
                )
                self._power.stop(
                    slug,
                    environment,
                    Actor.system("auto-stop"),
                    audit_action="environment.auto_stop",
                    audit_details={
                        "hours": state.auto_stop_hours or 0,
                        "reference_time": reference.isoformat(),
                    },
                )
            except Exception:
                logger.exception("auto-stop failed", extra={"project": slug})

    def run_forever(self, shutdown: StopSignal) -> None:
        """Run scans until the worker is shutting down."""
        while not shutdown.wait(self._interval):
            self.tick()
