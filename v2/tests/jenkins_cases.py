"""All pipelines compared with real Jenkins, plus the reasons for deliberate differences."""

from __future__ import annotations

from pathlib import Path

from shiftcodes.domain.diagnostics import Severity
from shiftcodes.pipeline import PipelineSyntaxError, ValidationContext, parse, serialize, validate
from shiftcodes.pipeline.templates import load_templates
from tests.jenkins_probes import PROBES
from tests.pipeline_support import FIXTURES

ROOT = Path(__file__).resolve().parent.parent
VERDICTS = Path(__file__).parent / "fixtures" / "jenkins_verdicts.json"
CONTEXT = ValidationContext(frozenset({"APP_KEY", "NPM_TOKEN", "DB_PASSWORD"}))

# Cases the platform rejects although the Jenkins linter accepts them, with the reason.
REASONS = {
    "PLAT": "platform rule (semantic check the Jenkins linter does not perform)",
    "SAFE": "platform safety policy",
    "FEAT": "unsupported Jenkins feature (platform scope)",
    "GROOVY": "free Groovy or code outside pipeline is not supported",
    "AMBIG": "ambiguous Groovy, rejected on purpose",
    "LIMIT": "platform limit",
    "STRICT": "stricter than Jenkins on purpose (no use case)",
}

POLICY: dict[str, str] = {
    "invalid/bad_env_name": REASONS["PLAT"],
    "invalid/code_after_pipeline": REASONS["GROOVY"],
    "invalid/code_before_pipeline": REASONS["GROOVY"],
    "invalid/deploy_before_build": REASONS["PLAT"],
    "invalid/deploy_unbuilt_image": REASONS["PLAT"],
    "invalid/parallel_depends_on_sibling": REASONS["PLAT"],
    "invalid/docker_network_host": REASONS["SAFE"],
    "invalid/docker_privileged": REASONS["SAFE"],
    "invalid/docker_volume": REASONS["SAFE"],
    "invalid/dockerfile_dotdot": REASONS["SAFE"],
    "invalid/path_dotdot": REASONS["SAFE"],
    "invalid/external_image_without_tag": REASONS["SAFE"],
    "invalid/import_statement": REASONS["GROOVY"],
    "invalid/library_annotation": REASONS["GROOVY"],
    "invalid/method_call_interpolation": REASONS["GROOVY"],
    "invalid/nesting_too_deep": REASONS["LIMIT"],
    "invalid/non_empty_triggers": REASONS["FEAT"],
    "invalid/retry_out_of_range": REASONS["PLAT"],
    "invalid/return_stdout": REASONS["GROOVY"],
    "invalid/script_block": REASONS["GROOVY"],
    "invalid/sh_without_docker_agent": REASONS["SAFE"],
    "invalid/unknown_credential": REASONS["PLAT"],
    "invalid/unknown_parameter": REASONS["PLAT"],
    "invalid/unknown_variable": REASONS["PLAT"],
    "invalid/unknown_step": REASONS["PLAT"],
    "invalid/when_expression": REASONS["GROOVY"],
    "probe/when beforeAgent": REASONS["FEAT"],
    "probe/when branch pattern named": REASONS["FEAT"],
    "probe/dir cmd syntax with block": REASONS["AMBIG"],
    "probe/dollar name dot": REASONS["AMBIG"],
    "probe/underscore int": REASONS["STRICT"],
    "probe/negative int": REASONS["STRICT"],
    "probe/env int value": REASONS["STRICT"],
    "probe/env bool value": REASONS["STRICT"],
    "probe/docker label option": REASONS["FEAT"],
    "probe/docker reuseNode": REASONS["FEAT"],
    "probe/agent label": REASONS["FEAT"],
    "probe/empty stage name": REASONS["PLAT"],
    "probe/timeout unit DAYS": REASONS["PLAT"],
    "probe/option timeout DAYS": REASONS["PLAT"],
    "probe/timeout no block": REASONS["PLAT"],
    "probe/retry no block": REASONS["PLAT"],
    "probe/dir no block": REASONS["PLAT"],
    "probe/param unknown type": REASONS["FEAT"],
    "probe/param empty choices": REASONS["PLAT"],
    "probe/param dup": REASONS["PLAT"],
    "probe/param bool string default": REASONS["PLAT"],
    "probe/stage unknown section input": REASONS["FEAT"],
    "probe/env lowercase": REASONS["PLAT"],
    "probe/two pipelines": REASONS["GROOVY"],
    "probe/script in post": REASONS["GROOVY"],
    "probe/string concat": REASONS["GROOVY"],
    "probe/env in steps unknown var": REASONS["PLAT"],
    "probe/mixed list": REASONS["PLAT"],
    "probe/list of lists": REASONS["PLAT"],
    "probe/bool arg": REASONS["GROOVY"],
    "probe/failFast in parallel": REASONS["FEAT"],
    "probe/retry paren newline brace step": REASONS["AMBIG"],
    "probe/unknown step with block": REASONS["PLAT"],
    "probe/param name invalid": REASONS["PLAT"],
    "probe/choices non string": REASONS["PLAT"],
    "probe/credentials two args": REASONS["STRICT"],
    "probe/docker image empty": REASONS["PLAT"],
    "probe/docker image int": REASONS["PLAT"],
    "probe/retry zero": REASONS["PLAT"],
}


def readme_example() -> str:
    """The first groovy block of the README pipelines section."""
    section = (ROOT / "README.md").read_text().split("## Pipelines (Jenkinsfile)", 1)[1]
    return section.split("```groovy\n", 1)[1].split("```", 1)[0]


def all_cases() -> dict[str, str]:
    """Every compared pipeline text by display name."""
    cases: dict[str, str] = {}
    for kind in ("valid", "invalid"):
        for path in sorted((FIXTURES / kind).glob("*.groovy")):
            cases[f"{kind}/{path.stem}"] = path.read_text()
    cases["readme/example"] = readme_example()
    for template in load_templates():
        cases[f"template/{template.name}"] = template.jenkinsfile
    for name, text in list(cases.items()):
        if name.startswith(("valid/", "readme/", "template/")):
            cases[f"serialized/{name}"] = serialize(parse(text))
    cases.update({f"probe/{name}": text for name, text in PROBES.items()})
    return cases


def our_verdict(text: str) -> str:
    """``accepted`` or ``rejected: <first error>``."""
    try:
        errors = [d for d in validate(parse(text), CONTEXT) if d.severity is Severity.ERROR]
    except PipelineSyntaxError as error:
        return f"rejected: {error.message}"
    return f"rejected: {errors[0].message}" if errors else "accepted"
