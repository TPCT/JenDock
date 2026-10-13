"""Jenkinsfile engine: parse, validate and serialize a Declarative Pipeline subset.

Pure code: nothing here executes Groovy, shells out or touches the network.
"""

from __future__ import annotations

from backend.pipeline.ast import Pipeline
from backend.pipeline.errors import Diagnostic, PipelineSyntaxError, Severity, Span
from backend.pipeline.parser import parse
from backend.pipeline.serializer import serialize
from backend.pipeline.validator import ValidationContext, validate

__all__ = [
    "Diagnostic",
    "Pipeline",
    "PipelineSyntaxError",
    "Severity",
    "Span",
    "ValidationContext",
    "parse",
    "serialize",
    "validate",
]
