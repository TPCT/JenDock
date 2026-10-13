"""Helpers shared by the Jenkinsfile engine tests."""

from __future__ import annotations

from pathlib import Path

from backend.domain.diagnostics import Diagnostic, Severity
from backend.pipeline import Pipeline, ValidationContext, parse, validate

FIXTURES = Path(__file__).parent / "fixtures" / "jenkinsfiles"
SECRETS = frozenset({"APP_KEY", "NPM_TOKEN", "DB_PASSWORD"})
CONTEXT = ValidationContext(SECRETS)
DOCKER_AGENT = "agent { docker { image 'node:22' } }"


def wrap(steps: str, agent: str = DOCKER_AGENT, extra: str = "") -> str:
    """A one-stage pipeline whose steps section is ``steps``."""
    head = f"pipeline {{\n{agent}\n{extra}\n"
    return head + f"stages {{\nstage('S') {{\nsteps {{\n{steps}\n}}\n}}\n}}\n}}\n"


def check(text: str, context: ValidationContext = CONTEXT) -> list[Diagnostic]:
    """Parse and validate; returns all diagnostics."""
    return validate(parse(text), context)


def errors(text: str, context: ValidationContext = CONTEXT) -> list[str]:
    """Messages of the error diagnostics of ``text``."""
    return [d.message for d in check(text, context) if d.severity is Severity.ERROR]


def parse_wrapped(steps: str) -> Pipeline:
    """Parse ``wrap(steps)``."""
    return parse(wrap(steps))
