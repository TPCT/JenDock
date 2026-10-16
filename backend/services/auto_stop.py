"""Periodic stop of idle development environments."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Protocol

from backend.containers.power import EnvironmentPower, EnvironmentPowerService, PowerState
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
                if not self._due(state, current):
                    continue
                details: dict[str, object] = {}

                def still_due(
                    fresh: EnvironmentPower, detail_values: dict[str, object] = details
                ) -> bool:
                    # The stop service calls this with a fresh snapshot while holding the
                    # environment lock, after any in-flight deployment has completed.
                    if not self._due(fresh, current):
                        return False
                    reference = max(
                        item
                        for item in (fresh.started_at, fresh.last_deployed_at)
                        if item is not None
                    )
                    detail_values.update(
                        {"hours": fresh.auto_stop_hours, "reference_time": reference.isoformat()}
                    )
                    return True

                self._power.stop(
                    slug,
                    environment,
                    Actor.system("auto-stop"),
                    audit_action="environment.auto_stop",
                    audit_details=details,
                    only_if=still_due,
                )
            except Exception:
                logger.exception("auto-stop failed", extra={"project": slug})

    @staticmethod
    def _due(state: EnvironmentPower, now: datetime) -> bool:
        """Whether a power snapshot is running and its configured deadline has passed."""
        return (
            state.state is PowerState.RUNNING
            and state.auto_stop_due_at is not None
            and now >= state.auto_stop_due_at
        )

    def run_forever(self, shutdown: StopSignal) -> None:
        """Run scans until the worker is shutting down."""
        while not shutdown.wait(self._interval):
            self.tick()
