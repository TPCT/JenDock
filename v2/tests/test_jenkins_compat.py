"""Offline replay of the verdicts recorded from a real Jenkins (``make lint-jenkins``)."""

import json

import pytest

from tests.jenkins_cases import POLICY, VERDICTS, all_cases, our_verdict

CASES = all_cases()
RECORDED: dict[str, bool] = json.loads(VERDICTS.read_text())


def test_every_case_has_a_recorded_jenkins_verdict() -> None:
    assert set(CASES) == set(RECORDED), "rerun `make lint-jenkins` to refresh the verdicts"


@pytest.mark.parametrize("name", sorted(CASES))
def test_we_never_accept_what_jenkins_rejects(name: str) -> None:
    if not RECORDED[name]:
        assert our_verdict(CASES[name]) != "accepted"


@pytest.mark.parametrize(
    "name", sorted(n for n in CASES if n.startswith(("valid/", "readme/", "serialized/")))
)
def test_valid_pipelines_and_serializer_output_pass_jenkins(name: str) -> None:
    assert RECORDED[name]
    assert our_verdict(CASES[name]) == "accepted"


def test_differences_are_exactly_the_documented_policy() -> None:
    differing = {
        name for name, text in CASES.items() if RECORDED[name] and our_verdict(text) != "accepted"
    }
    assert differing == set(POLICY)
