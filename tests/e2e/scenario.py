"""State and helpers shared by the steps of the end-to-end story."""

from __future__ import annotations

import urllib.request
from dataclasses import dataclass, field
from typing import Any

from tests.e2e.sample import SampleProject, SampleRepo
from tests.e2e.stack import Stack

SLUG = "shop"
ENV = f"/api/projects/{SLUG}/environments/development"
PROD = f"/api/projects/{SLUG}/environments/production"


@dataclass(slots=True)
class Scenario:
    """What earlier steps created (the tests are steps of one story, run in file order)."""

    repo: SampleRepo | None = None
    project: SampleProject | None = None
    token: str = ""
    shas: dict[str, str] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)

    def sample(self) -> SampleProject:
        assert self.project is not None, "the sample project was not created"
        return self.project

    def source(self) -> SampleRepo:
        assert self.repo is not None, "the repository was not created"
        return self.repo


def web_version(stack: Stack) -> str:
    """What the deployed web service answers on the port from the env file (the host side)."""
    url = f"http://127.0.0.1:{stack.web_port}/"
    with urllib.request.urlopen(url, timeout=10) as response:
        return str(response.read().decode().strip())


def container_ids(project: SampleProject) -> dict[str, str]:
    """Container id per service of the sample compose project."""
    return {s: project.compose("ps", "-q", s).strip() for s in ("web", "worker", "db")}


def image_of(stack: Stack, container_id: str) -> str:
    """The image reference a container was created from."""
    return stack.docker("inspect", "--format", "{{.Config.Image}}", container_id).stdout.strip()
