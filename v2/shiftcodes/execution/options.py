"""Pipeline and stage ``options``: ``timeout`` and ``retry``."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from shiftcodes.execution.errors import StepFailed
from shiftcodes.execution.retry import run_with_retries
from shiftcodes.execution.scope import Scope
from shiftcodes.execution.steps_basic import timeout_seconds
from shiftcodes.pipeline.ast import IntValue, StepCall, StringValue
from shiftcodes.pipeline.steps import arg_value


@dataclass(frozen=True, slots=True)
class StageOptions:
    """The options that change how a block of work is executed."""

    timeout: float | None = None
    retries: int = 1

    @classmethod
    def parse(cls, calls: Sequence[StepCall]) -> StageOptions:
        """Read ``timeout`` and ``retry`` (other options have no execution effect)."""
        timeout: float | None = None
        retries = 1
        for call in calls:
            if call.name == "timeout":
                timeout = _timeout(call)
            elif call.name == "retry":
                retries = _int(call, "count", 1)
        return cls(timeout, retries)

    def apply(
        self,
        scope: Scope,
        body: Callable[[Scope], None],
        on_retry: Callable[[int, StepFailed], None],
    ) -> None:
        """Run ``body`` with the deadline and retry policy of these options."""
        inner = scope.with_timeout(self.timeout) if self.timeout is not None else scope
        run_with_retries(self.retries, lambda _n: body(inner), on_retry)


def _int(call: StepCall, name: str, default: int) -> int:
    value = arg_value(call, name)
    return value.value if isinstance(value, IntValue) else default


def _timeout(call: StepCall) -> float:
    unit = arg_value(call, "unit")
    text = unit.plain if isinstance(unit, StringValue) and unit.plain else "MINUTES"
    return timeout_seconds(_int(call, "time", 1), text)
