"""A small world for the access tests: two projects, runs and one user per kind of grant."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import Engine

from backend.api.container import AppContainer
from backend.domain.access import AccessRole, ContainerPermission
from backend.domain.enums import EnvironmentName, PipelineSource, RunTrigger
from backend.repositories.models import Run
from backend.services.projects import ProjectService
from tests.access_support import (
    FakeContainers,
    Session,
    add_user,
    grant,
    sign_in,
    with_fake_containers,
)
from tests.api_support import FakeClock, create_user, make_container, make_settings
from tests.github_support import SHA_A, SHA_B, FakeGithub

DEV, PROD = EnvironmentName.DEVELOPMENT, EnvironmentName.PRODUCTION
V, D, M = AccessRole.VIEWER, AccessRole.DEVELOPER, AccessRole.MAINTAINER
LOGS, TERMINAL, ACTIONS = (
    ContainerPermission.LOGS,
    ContainerPermission.TERMINAL,
    ContainerPermission.ACTIONS,
)


@dataclass
class World:
    """Two projects, runs, one user per kind of grant and a fake container gateway."""

    container: AppContainer
    fake: FakeContainers
    owner: Session
    runs: dict[str, int]
    users: dict[str, Session]

    def __getitem__(self, name: str) -> Session:
        return self.users[name]


def _add_run(container: AppContainer, slug: str, env: EnvironmentName, number: int) -> int:
    with container.uow() as uow:
        environment = uow.environments.get_by_project_slug(slug, env)
        assert environment is not None
        row = uow.runs.add(
            Run(
                environment_id=environment.id,
                number=number,
                trigger=RunTrigger.MANUAL,
                branch=environment.branch,
                commit_sha=SHA_A,
                pipeline_source=PipelineSource.PLATFORM,
                pipeline_text="pipeline {}",
                created_at=container.clock(),
            )
        )
        return row.id


@pytest.fixture
def world(tmp_path: Path, engine: Engine, clock: FakeClock) -> World:
    github = FakeGithub({"development": SHA_A, "production": SHA_B})
    base = make_container(make_settings(tmp_path), engine, clock, github=github)
    container, fake = with_fake_containers(base)
    create_user(container)
    for slug in ("demo", "other"):
        ProjectService(container.uow()).create(slug, slug.title(), f"octo/{slug}")
    runs = {
        "demo_dev": _add_run(container, "demo", DEV, 1),
        "demo_prod": _add_run(container, "demo", PROD, 1),
        "other_dev": _add_run(container, "other", DEV, 1),
    }
    members = {
        "viewer": ("demo", {"role": V}),
        "developer": ("demo", {"role": D}),
        "maintainer": ("demo", {"role": M}),
        "devonly": ("demo", {"role": V, "environment": DEV}),
        "logs": ("demo", {"environment": DEV, "service": "web", "permissions": [LOGS]}),
        "actions": ("demo", {"environment": DEV, "service": "web", "permissions": [ACTIONS]}),
        "terminal": ("demo", {"environment": DEV, "service": "web", "permissions": [TERMINAL]}),
        "nobody": (None, {}),
    }
    users: dict[str, Session] = {}
    for name, (project, kwargs) in members.items():
        user_id = add_user(container, name)
        if project:
            grant(container, project, user_id, **kwargs)  # type: ignore[arg-type]
        users[name] = sign_in(container, name)
    return World(container, fake, sign_in(container, "admin"), runs, users)
