"""``PipelineValidator`` backed by the Jenkinsfile engine."""

from __future__ import annotations

from backend.domain.diagnostics import Severity
from backend.domain.errors import ValidationError
from backend.pipeline import PipelineSyntaxError, ValidationContext, parse, validate
from backend.pipeline.errors import Diagnostic


class JenkinsfileValidator:
    """Rejects Jenkinsfile text with syntax or validation errors (warnings are allowed)."""

    def __init__(self, context: ValidationContext | None = None) -> None:
        self._context = context or ValidationContext()

    def validate(self, jenkinsfile: str) -> None:
        """Raise ``ValidationError`` carrying all error diagnostics; return when valid."""
        try:
            pipeline = parse(jenkinsfile)
        except PipelineSyntaxError as error:
            diagnostic = Diagnostic(Severity.ERROR, error.message, error.span)
            raise ValidationError(str(error), [diagnostic]) from error
        errors = [d for d in validate(pipeline, self._context) if d.severity is Severity.ERROR]
        if errors:
            count = len(errors)
            first = errors[0]
            where = f"line {first.span.line}, column {first.span.column}"
            summary = f"pipeline has {count} error(s); first at {where}: {first.message}"
            raise ValidationError(summary, errors)
