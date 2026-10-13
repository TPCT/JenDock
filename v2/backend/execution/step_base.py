"""Base class of step implementations and helpers to read their arguments."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from backend.execution.context import StepContext
from backend.execution.errors import StepFailed
from backend.pipeline.ast import IntValue, ListValue, MapValue, StepCall, StringValue, Value
from backend.pipeline.steps import arg_value

MAX_SUMMARY = 120


class Step(ABC):
    """One step call. Subclasses are registered by ``name`` in ``step_registry``."""

    name: ClassVar[str]

    def __init__(self, call: StepCall) -> None:
        self.call = call

    @abstractmethod
    def execute(self, ctx: StepContext) -> None:
        """Run the step; raise ``StepFailed`` (or ``RunCancelled``) to end it unsuccessfully."""

    def summary(self) -> str:
        """Short description for the step list; built from the source text, never from
        interpolated values, so it cannot contain a secret."""
        return ""

    def label(self) -> str:
        """The step's display label."""
        text = " ".join(self.summary().split())
        text = text if len(text) <= MAX_SUMMARY else text[: MAX_SUMMARY - 3] + "..."
        return f"{self.name}: {text}" if text else self.name

    # --- argument access ----------------------------------------------------------

    def value(self, name: str) -> Value | None:
        """The raw value of an argument."""
        return arg_value(self.call, name)

    def string(self, name: str) -> StringValue | None:
        """A string argument, if present."""
        value = self.value(name)
        return value if isinstance(value, StringValue) else None

    def required_string(self, name: str) -> StringValue:
        """A string argument that must be present."""
        value = self.string(name)
        if value is None:
            raise StepFailed(f"'{self.name}' needs argument '{name}'")
        return value

    def integer(self, name: str, default: int) -> int:
        """An integer argument or ``default``."""
        value = self.value(name)
        return value.value if isinstance(value, IntValue) else default

    def items(self, name: str) -> tuple[Value, ...]:
        """The elements of a list argument."""
        value = self.value(name)
        return value.items if isinstance(value, ListValue) else ()

    def mapping(self, name: str) -> dict[str, StringValue]:
        """The string entries of a map argument."""
        value = self.value(name)
        if not isinstance(value, MapValue):
            return {}
        return {e.key: e.value for e in value.entries if isinstance(e.value, StringValue)}
