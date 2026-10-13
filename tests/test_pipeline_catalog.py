"""The generated catalogue and the starter templates."""

from __future__ import annotations

import pytest

from backend.pipeline import ValidationContext, parse, serialize, validate
from backend.pipeline.ast import PostCondition
from backend.pipeline.catalog import ArgType, StepInfo, build_catalog
from backend.pipeline.errors import Severity
from backend.pipeline.steps import OPTIONS, PARAMETERS, STEPS, ImageRole
from backend.pipeline.templates import Template, load_templates

CATALOG = build_catalog()
SECTIONS = {"steps": STEPS, "options": OPTIONS, "parameters": PARAMETERS}
ALL: list[tuple[str, StepInfo]] = [
    (section, info) for section in SECTIONS for info in getattr(CATALOG, section)
]


def _by_name(section: str) -> dict[str, StepInfo]:
    return {info.name: info for info in getattr(CATALOG, section)}


@pytest.mark.parametrize("section", sorted(SECTIONS))
def test_every_registry_entry_is_in_the_catalogue(section: str) -> None:
    assert list(_by_name(section)) == list(SECTIONS[section])


@pytest.mark.parametrize(("section", "info"), ALL, ids=lambda v: getattr(v, "name", v))
def test_every_step_has_title_and_description(section: str, info: StepInfo) -> None:
    assert info.title.strip(), f"{section}.{info.name} has no title"
    assert info.description.strip().endswith("."), f"{section}.{info.name} needs a sentence"
    assert len(info.title) <= 40


@pytest.mark.parametrize(("section", "info"), ALL, ids=lambda v: getattr(v, "name", v))
def test_every_argument_has_help(section: str, info: StepInfo) -> None:
    for arg in info.args:
        assert arg.description.strip(), f"{section}.{info.name}.{arg.name} has no description"


def test_catalogue_args_come_from_the_registry_without_rejected_ones() -> None:
    for section, registry in SECTIONS.items():
        for name, info in _by_name(section).items():
            expected = [spec.name for spec in registry[name].args if spec.rejected is None]
            assert [arg.name for arg in info.args] == expected
    sh = _by_name("steps")["sh"]
    assert "returnStdout" not in [arg.name for arg in sh.args]


def test_enums_bounds_defaults_and_roles_are_derived_from_the_checks() -> None:
    steps = _by_name("steps")
    unit = next(arg for arg in steps["timeout"].args if arg.name == "unit")
    assert unit.choices == ("SECONDS", "MINUTES", "HOURS")
    assert unit.default == "MINUTES"
    time = next(arg for arg in steps["timeout"].args if arg.name == "time")
    assert (time.minimum, time.maximum, time.type, time.required) == (1, None, ArgType.INT, True)
    count = steps["retry"].args[0]
    assert (count.minimum, count.maximum) == (1, 10)
    status = next(arg for arg in steps["healthCheck"].args if arg.name == "expectStatus")
    assert (status.minimum, status.maximum, status.default) == (100, 599, 200)
    roles = {
        (name, arg.name): arg.role
        for name, info in steps.items()
        for arg in info.args
        if arg.role is not None
    }
    assert roles == {
        ("dockerBuild", "image"): ImageRole.PRODUCES,
        ("deploy", "image"): ImageRole.USES,
        ("deploy", "services"): ImageRole.USES,
    }
    assert [arg.name for arg in steps["sh"].args if arg.multiline] == ["script"]


def test_block_and_container_flags() -> None:
    steps = _by_name("steps")
    assert {name for name, info in steps.items() if info.takes_block} == {
        "timeout",
        "retry",
        "dir",
        "withEnv",
    }
    assert {name for name, info in steps.items() if info.needs_container} == {"sh"}
    assert not _by_name("options")["timeout"].takes_block


def test_post_conditions_are_all_described_in_conventional_order() -> None:
    assert [item.name for item in CATALOG.post_conditions] == list(PostCondition)
    assert all(item.description for item in CATALOG.post_conditions)


# --- templates -------------------------------------------------------------------------------


def test_there_is_a_template_for_each_supported_project_type() -> None:
    assert [t.name for t in load_templates()] == [
        "laravel",
        "nextjs",
        "node-api",
        "python-fastapi",
        "static-site",
        "dockerfile",
    ]


@pytest.mark.parametrize("template", load_templates(), ids=lambda t: t.name)
def test_template_is_valid_without_any_secret(template: Template) -> None:
    text = template.jenkinsfile
    pipeline = parse(text)
    findings = validate(pipeline, ValidationContext())
    assert [d for d in findings if d.severity is Severity.ERROR] == []
    assert parse(serialize(pipeline)) == pipeline


@pytest.mark.parametrize("template", load_templates(), ids=lambda t: t.name)
def test_template_is_described_and_tells_the_user_what_to_change(template: Template) -> None:
    assert template.title
    assert template.description.endswith(".")
    assert template.jenkinsfile.startswith("//")
