"""Mutation fuzzing: the engine must fail only with ``PipelineSyntaxError`` and stay stable."""

import random

from shiftcodes.pipeline import PipelineSyntaxError, parse, serialize, validate
from tests.pipeline_support import CONTEXT, FIXTURES

SOURCES = [path.read_text() for path in sorted((FIXTURES / "valid").glob("*.groovy"))]
JUNK = ["'", '"', "{", "}", "(", ")", "[", "]", "$", "${", "\\", ",", ":", "=", ";", "\n", "x", "1"]


def mutate(rng: random.Random, text: str) -> str:
    for _ in range(rng.randint(1, 4)):
        position = rng.randrange(len(text))
        if rng.random() < 0.5:
            text = text[:position] + text[position + rng.randint(1, 6) :]
        else:
            text = text[:position] + rng.choice(JUNK) + text[position:]
    return text


def test_mutations_never_crash_and_parsed_asts_round_trip() -> None:
    rng = random.Random(7)  # noqa: S311 - deterministic test data
    parsed = 0
    for _ in range(1500):
        text = mutate(rng, rng.choice(SOURCES))
        try:
            pipeline = parse(text)
        except PipelineSyntaxError as error:
            assert error.span.line >= 1
            assert error.span.column >= 1
            continue
        parsed += 1
        validate(pipeline, CONTEXT)
        assert parse(serialize(pipeline)) == pipeline
    assert parsed > 0
