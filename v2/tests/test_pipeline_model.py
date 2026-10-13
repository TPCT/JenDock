"""The editing model: AST <-> model round trips, template text, rendering half-filled models."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError as PydanticValidationError

from shiftcodes.pipeline import ValidationContext, parse, serialize
from shiftcodes.pipeline.analysis import analyze, render, to_editing_model
from shiftcodes.pipeline.ast import StringPart, TextPart, VariablePart, VarSource
from shiftcodes.pipeline.errors import Severity, Span
from shiftcodes.pipeline.model import Call, Model, TemplateText
from shiftcodes.pipeline.model_from_ast import to_model
from shiftcodes.pipeline.model_template import decode, encode
from shiftcodes.pipeline.model_to_ast import to_ast
from tests.pipeline_support import CONTEXT, FIXTURES, wrap

VALID = sorted((FIXTURES / "valid").glob("*.groovy"))
SPAN = Span(1, 1)


def _json_round_trip(value: Model) -> Model:
    """The model as the browser sees it: dumped to JSON and read back."""
    return Model.model_validate(json.loads(json.dumps(value.model_dump(mode="json"))))


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.stem)
def test_ast_to_model_to_ast_is_identity(path: Path) -> None:
    pipeline = parse(path.read_text())
    assert to_ast(to_model(pipeline)) == pipeline


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.stem)
def test_model_survives_json_and_renders_canonical_text(path: Path) -> None:
    pipeline = parse(path.read_text())
    model = _json_round_trip(to_model(pipeline))
    assert model == to_model(pipeline)
    assert render(model, CONTEXT).jenkinsfile == serialize(pipeline)


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.stem)
def test_rendered_text_of_valid_fixture_has_no_errors(path: Path) -> None:
    rendering = render(to_model(parse(path.read_text())), CONTEXT)
    assert [d for d in rendering.diagnostics if d.severity is Severity.ERROR] == []


def test_model_json_follows_the_contract() -> None:
    text = wrap("sh 'npm ci'\ntimeout(time: 5) {\necho 'x'\n}\ndeploy services: [web: 'img']")
    body = to_model(parse(text)).model_dump(mode="json")
    assert body["agent"] == {"type": "docker", "image": "node:22", "args": None}
    assert body["post"] is None
    assert body["parameters"] == []
    stage = body["stages"][0]
    assert stage["when"] is None
    assert stage["body"]["type"] == "steps"
    sh, timeout, deploy = stage["body"]["steps"]
    assert sh == {"name": "sh", "args": {"script": "npm ci"}}  # no body key without a block
    assert timeout["args"] == {"time": 5}
    assert timeout["body"] == [{"name": "echo", "args": {"message": "x"}}]
    assert deploy["args"] == {"services": {"map": [{"key": "web", "value": "img"}]}}


def test_double_quoted_string_without_variables_is_a_plain_string() -> None:
    pipeline = parse(wrap('echo "plain"\necho "hi ${BUILD_NUMBER} $BRANCH_NAME"'))
    first, second = to_model(pipeline).stages[0].body.steps  # type: ignore[union-attr]
    assert first.args["message"] == "plain"
    assert second.args["message"] == TemplateText(template="hi ${BUILD_NUMBER} ${BRANCH_NAME}")


def test_unnamed_arguments_of_unknown_steps_are_kept() -> None:
    pipeline = parse(wrap("mystery 'a', 'b', k: 3"))
    call = to_model(pipeline).stages[0].body.steps[0]  # type: ignore[union-attr]
    assert call.args == {"$0": "a", "$1": "b", "k": 3}
    assert to_ast(to_model(pipeline)) == pipeline


def test_positional_argument_is_normalised_to_its_name() -> None:
    call = to_model(parse(wrap("sh 'ls'\nretry(3) { sh 'x' }"))).stages[0].body.steps  # type: ignore[union-attr]
    assert call[0].args == {"script": "ls"}
    assert call[1].args == {"count": 3}


def test_bool_and_int_values_keep_their_type() -> None:
    value = Model.model_validate(
        {
            "stages": [
                {
                    "name": "S",
                    "body": {
                        "type": "steps",
                        "steps": [{"name": "x", "args": {"a": True, "b": 1, "c": [1, False]}}],
                    },
                }
            ]
        }
    )
    assert to_model(to_ast(value)) == value
    args = to_model(to_ast(value)).stages[0].body.steps[0].args  # type: ignore[union-attr]
    assert args["a"] is True
    assert args["b"] == 1
    assert args["b"] is not True
    assert args["c"] == [1, False]


# --- template text ---------------------------------------------------------------------------


def _text(*parts: str | tuple[VarSource, str]) -> tuple[StringPart, ...]:
    return tuple(
        TextPart(item) if isinstance(item, str) else VariablePart(item[0], item[1], SPAN)
        for item in parts
    )


@pytest.mark.parametrize(
    "parts",
    [
        _text("a ", (VarSource.BARE, "X"), " b"),
        _text((VarSource.ENV, "HOME"), (VarSource.PARAMS, "target")),
        _text("cost: $5 and C:\\dir \\$ \\\\"),
        _text((VarSource.EXPRESSION, "a.b.c")),
        _text("quote \" and ' ok"),
    ],
)
def test_template_text_round_trips(parts: tuple[StringPart, ...]) -> None:
    assert decode(encode(parts)) == parts


def test_decode_never_fails_on_half_typed_templates() -> None:
    assert decode("price $") == (TextPart("price $"),)
    assert decode("open ${NAME") == (TextPart("open ${NAME"),)
    assert decode("$1 x") == (TextPart("$1 x"),)
    assert decode("") == ()
    assert decode("$NAME and ${ env.NAME }") == (
        VariablePart(VarSource.BARE, "NAME", SPAN),
        TextPart(" and "),
        VariablePart(VarSource.ENV, "NAME", SPAN),
    )


# --- rendering half-filled models ------------------------------------------------------------


def test_half_filled_model_renders_and_reports_what_is_missing() -> None:
    model = Model.model_validate(
        {
            "stages": [
                {
                    "name": "",
                    "body": {"type": "steps", "steps": [{"name": "sh", "args": {}}]},
                }
            ]
        }
    )
    rendering = render(model, ValidationContext())
    assert "stage('')" in rendering.jenkinsfile
    messages = [d.message for d in rendering.diagnostics]
    assert any("missing required 'agent'" in m for m in messages)
    assert any("stage name must not be empty" in m for m in messages)
    assert any("requires argument 'script'" in m for m in messages)
    assert all(d.span.line >= 1 for d in rendering.diagnostics)


def test_empty_model_renders() -> None:
    rendering = render(Model(), ValidationContext())
    assert rendering.jenkinsfile.startswith("pipeline {")
    assert any("at least one stage" in d.message for d in rendering.diagnostics)


def test_untidy_values_render_without_crashing() -> None:
    model = Model.model_validate(
        {
            "agent": {"type": "any"},
            "stages": [
                {
                    "name": "a b",
                    "body": {
                        "type": "steps",
                        "steps": [
                            {"name": "echo", "args": {"message": {"template": "$ ${"}}},
                            {"name": "weird name", "args": {"bad key": 1}},
                        ],
                    },
                }
            ],
        }
    )
    rendering = render(model, ValidationContext())
    assert rendering.jenkinsfile
    assert rendering.diagnostics  # a syntax error or a validation error, positioned


def test_invalid_model_shape_is_rejected_by_the_schema() -> None:
    for bad in (
        {"agent": {"type": "weird"}},
        {"stages": [{"name": "x", "body": {"type": "steps"}}]},
        {"stages": "nope"},
        {"extra": 1},
        {"environment": [{"name": "A", "value": 1.5}]},
    ):
        with pytest.raises(PydanticValidationError):
            Model.model_validate(bad)


def test_call_without_block_omits_body_key() -> None:
    assert Call(name="echo").model_dump(mode="json") == {"name": "echo", "args": {}}
    assert Call(name="dir", body=[]).model_dump(mode="json")["body"] == []


def test_analyze_syntax_error_has_one_diagnostic_and_no_model() -> None:
    analysis = analyze("pipeline {", CONTEXT)
    assert analysis.pipeline is None
    assert len(analysis.diagnostics) == 1
    assert to_editing_model(analysis) is None


def test_analyze_semantic_error_still_gives_a_model() -> None:
    analysis = analyze(wrap("sh 'x'\nfoo 'y'", agent="agent any"), CONTEXT)
    assert to_editing_model(analysis) is not None
    assert any("unknown step 'foo'" in d.message for d in analysis.diagnostics)
