"""Jenkinsfile engine: parse, validate and serialize a Declarative Pipeline subset.

Pure code: nothing here executes Groovy, shells out or touches the network.
"""

from __future__ import annotations

from shiftcodes.pipeline.ast import Pipeline
from shiftcodes.pipeline.errors import Diagnostic, PipelineSyntaxError, Severity, Span
from shiftcodes.pipeline.parser import parse
from shiftcodes.pipeline.serializer import serialize
from shiftcodes.pipeline.validator import ValidationContext, validate

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
