"""Whole-text operations of the editors: diagnose text, render a model, parse into a model.

Everything returns diagnostics instead of raising, because an editor must show problems next to a
half-finished pipeline rather than refuse to work with it.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.pipeline import model
from backend.pipeline.ast import Pipeline
from backend.pipeline.errors import Diagnostic, PipelineSyntaxError, Severity
from backend.pipeline.model_from_ast import to_model
from backend.pipeline.model_to_ast import to_ast
from backend.pipeline.parser import parse
from backend.pipeline.serializer import serialize
from backend.pipeline.validator import ValidationContext, validate


@dataclass(frozen=True, slots=True)
class Analysis:
    """The result of reading a text: the AST (``None`` after a syntax error) and findings."""

    pipeline: Pipeline | None
    diagnostics: tuple[Diagnostic, ...]


@dataclass(frozen=True, slots=True)
class Rendering:
    """The canonical text of a model and the findings about it."""

    jenkinsfile: str
    diagnostics: tuple[Diagnostic, ...]


def syntax_diagnostic(error: PipelineSyntaxError) -> Diagnostic:
    """The single diagnostic that stands for a syntax error."""
    return Diagnostic(Severity.ERROR, error.message, error.span)


def analyze(text: str, context: ValidationContext) -> Analysis:
    """Parse and validate ``text``; a syntax error yields exactly one diagnostic."""
    try:
        pipeline = parse(text)
    except PipelineSyntaxError as error:
        return Analysis(None, (syntax_diagnostic(error),))
    return Analysis(pipeline, tuple(validate(pipeline, context)))


def render(value: model.Model, context: ValidationContext) -> Rendering:
    """Canonical text of ``value``, validated as written (so positions refer to the text).

    Half-filled models still render: the serializer accepts any AST, and what is wrong with it is
    reported as diagnostics.
    """
    text = serialize(to_ast(value))
    return Rendering(text, analyze(text, context).diagnostics)


def to_editing_model(analysis: Analysis) -> model.Model | None:
    """The editing model of an analysed text, or ``None`` after a syntax error."""
    return None if analysis.pipeline is None else to_model(analysis.pipeline)
