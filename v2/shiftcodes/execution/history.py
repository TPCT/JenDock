"""``RunHistory`` backed by the run lifecycle service."""

from __future__ import annotations

from shiftcodes.domain.enums import RunStatus
from shiftcodes.services.run_lifecycle import PreviousRun, RunLifecycleService


class LifecycleHistory:
    """The previous finished run of the same environment, loaded once."""

    def __init__(self, lifecycle: RunLifecycleService, run_id: int) -> None:
        self._lifecycle = lifecycle
        self._run_id = run_id
        self._loaded = False
        self._previous: PreviousRun | None = None

    def _previous_run(self) -> PreviousRun | None:
        if not self._loaded:
            self._previous = self._lifecycle.previous_run(self._run_id)
            self._loaded = True
        return self._previous

    def previous_run_status(self) -> RunStatus | None:
        """Status of the latest finished run before this one."""
        previous = self._previous_run()
        return previous.status if previous else None

    def previous_stage_status(self, stage_name: str) -> RunStatus | None:
        """Status of the same stage in the previous run."""
        previous = self._previous_run()
        if previous is None:
            return None
        return self._lifecycle.previous_stage_status(previous.id, stage_name)
