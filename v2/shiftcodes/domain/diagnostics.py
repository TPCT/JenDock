"""Source positions and diagnostics shared by the pipeline engine and domain errors."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Severity(StrEnum):
    """How serious a diagnostic is; only errors reject a pipeline."""

    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, slots=True, order=True)
class Span:
    """1-based line and column of a node or token start."""

    line: int
    column: int


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One finding about a pipeline text, positioned in the source."""

    severity: Severity
    message: str
    span: Span
