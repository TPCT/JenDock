"""Fixtures of the end-to-end test: one real stack for the whole module, a timing report."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.docker_support import assert_nothing_left, docker_available
from tests.e2e.client import Api
from tests.e2e.scenario import Scenario
from tests.e2e.stack import ADMIN_USER, Stack

STEP_TIMINGS: dict[str, float] = {}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark everything below ``tests/e2e`` and skip it when Docker is missing."""
    skip = pytest.mark.skip(reason="no Docker daemon available")
    for item in items:
        if "e2e" in item.path.parts:
            item.add_marker(pytest.mark.e2e)
            if not docker_available():
                item.add_marker(skip)


@pytest.fixture(scope="session")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    """Build and start the real stack; remove everything afterwards and verify it."""
    root = tmp_path_factory.mktemp("e2e")
    root.chmod(0o755)
    built = Stack(root)
    try:
        started = time.monotonic()
        built.start()
        built.timings["stack ready"] = round(time.monotonic() - started, 1)
        yield built
    finally:
        try:
            built.stop()
        finally:
            assert_nothing_left()


@pytest.fixture(scope="session")
def api(stack: Stack) -> Api:
    """The admin's browser session."""
    session = Api(stack.base_url)
    session.login(ADMIN_USER, stack.admin_password)
    return session


@pytest.hookimpl(trylast=True)
def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    """Collect the duration of every end-to-end step."""
    if report.when == "call" and "tests/e2e" in report.nodeid:
        STEP_TIMINGS[report.nodeid.split("::")[-1]] = round(report.duration, 1)


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    """Print the timing report (and write it where ``E2E_REPORT`` points)."""
    if not STEP_TIMINGS:
        return
    lines = ["| step | seconds |", "|---|---|"]
    lines += [f"| {name} | {seconds} |" for name, seconds in STEP_TIMINGS.items()]
    lines.append(f"| total | {round(sum(STEP_TIMINGS.values()), 1)} |")
    terminalreporter.section("end-to-end timings")
    for line in lines:
        terminalreporter.write_line(line)
    target = os.environ.get("E2E_REPORT")
    if target:
        Path(target).write_text("\n".join(lines) + "\n")


@pytest.fixture(scope="session")
def scenario() -> Scenario:
    """State shared by the steps."""
    return Scenario()
