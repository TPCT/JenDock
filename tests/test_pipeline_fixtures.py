import re
from pathlib import Path

import pytest

from backend.domain.diagnostics import Severity
from backend.pipeline import PipelineSyntaxError, parse, serialize, validate
from tests.pipeline_support import CONTEXT, FIXTURES

VALID = sorted((FIXTURES / "valid").glob("*.groovy"))
INVALID = sorted((FIXTURES / "invalid").glob("*.groovy"))
_EXPECT = re.compile(r"// expect: line (\d+): (.+)")


def test_fixture_sets_are_not_empty() -> None:
    assert len(VALID) >= 9
    assert len(INVALID) >= 30


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.stem)
def test_valid_fixture_has_no_errors(path: Path) -> None:
    diagnostics = validate(parse(path.read_text()), CONTEXT)
    assert [d for d in diagnostics if d.severity is Severity.ERROR] == []


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.stem)
def test_round_trip_property(path: Path) -> None:
    pipeline = parse(path.read_text())
    canonical = serialize(pipeline)
    assert parse(canonical) == pipeline
    assert serialize(parse(canonical)) == canonical


@pytest.mark.parametrize("path", INVALID, ids=lambda p: p.stem)
def test_invalid_fixture_reports_expected_error(path: Path) -> None:
    text = path.read_text()
    match = _EXPECT.match(text.splitlines()[0])
    assert match, "fixture needs a '// expect: line N: message' header"
    line, fragment = int(match.group(1)), match.group(2)
    try:
        diagnostics = validate(parse(text), CONTEXT)
    except PipelineSyntaxError as error:
        found = [(error.span.line, error.message)]
    else:
        found = [(d.span.line, d.message) for d in diagnostics if d.severity is Severity.ERROR]
    assert found, "invalid fixture was accepted"
    assert found[0][0] == line
    assert fragment in found[0][1]


def test_docs_example_is_valid_and_canonical() -> None:
    section = (Path(__file__).parent.parent / "docs" / "pipelines.md").read_text()
    example = section.split("```groovy\n", 1)[1].split("```", 1)[0]
    pipeline = parse(example)
    assert [d for d in validate(pipeline, CONTEXT) if d.severity is Severity.ERROR] == []
    assert serialize(pipeline) != ""
