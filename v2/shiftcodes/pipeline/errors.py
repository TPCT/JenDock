"""Errors of the Jenkinsfile engine. Positions are 1-based."""

from __future__ import annotations

from shiftcodes.domain.diagnostics import Diagnostic, Severity, Span

__all__ = ["Diagnostic", "PipelineSyntaxError", "Severity", "Span", "syntax_error"]

_MAX_EXCERPT = 160


class PipelineSyntaxError(Exception):
    """The text is not a supported Jenkinsfile; carries position and a caret excerpt."""

    def __init__(self, message: str, span: Span, excerpt: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.span = span
        self.excerpt = excerpt

    def __str__(self) -> str:
        head = f"line {self.span.line}, column {self.span.column}: {self.message}"
        return f"{head}\n{self.excerpt}" if self.excerpt else head


def syntax_error(source: str, message: str, span: Span) -> PipelineSyntaxError:
    """Build an error for ``span`` with the source line and a caret under the column."""
    return PipelineSyntaxError(message, span, _excerpt(source, span))


def _excerpt(source: str, span: Span) -> str:
    lines = source.split("\n")
    if not 1 <= span.line <= len(lines):
        return ""
    line = lines[span.line - 1].rstrip("\r")
    start = max(0, span.column - 1 - _MAX_EXCERPT // 2)
    shown = line[start : start + _MAX_EXCERPT]
    padding = "".join(c if c == "\t" else " " for c in line[start : span.column - 1])
    return f"    {shown}\n    {padding}^"
