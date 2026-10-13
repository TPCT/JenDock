# ruff: noqa: F811
"""Container env edits next to a deployed image override, on a real Docker daemon.

The classic regression: after the deployer pinned a new image in its managed override, an env
edit recreates the service with the compose files again. The deployer's override must still be
the last ``-f`` (the service keeps the deployed image) and the compose env files must still be
passed (``${WEB_PORT:?}`` interpolation). Reuses the throwaway stack of ``test_deploy_docker``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import Engine

from shiftcodes.containers.locks import EnvironmentLocks, LockedDeployer, managed_files
from shiftcodes.domain.actor import Actor
from shiftcodes.domain.containers import EnvChange, LimitChange
from shiftcodes.domain.enums import ActorType, EnvironmentName
from shiftcodes.repositories.database import UnitOfWork, create_session_factory
from shiftcodes.services.environments import EnvironmentService, EnvironmentUpdate
from shiftcodes.worker.container_wiring import build_container_parts
from tests.api_support import make_settings
from tests.deploy_support import NotCancelled
from tests.docker_support import docker_available
from tests.test_deploy_docker import Stack, base_image, running, stack  # noqa: F401

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not docker_available(), reason="no Docker daemon available"),
]
DEV = EnvironmentName.DEVELOPMENT
ACTOR = Actor(ActorType.USER, "1")


def _env_of(stack: Stack, service: str) -> dict[str, str]:
    raw = stack.inspect(service, "{{range .Config.Env}}{{println .}}{{end}}")
    return dict(line.split("=", 1) for line in raw.splitlines() if "=" in line)


def test_env_edit_after_a_deployment_keeps_the_deployed_image(
    running: Stack, engine: Engine, project: str, socket_dir: Path
) -> None:
    live = running
    deployed = live.build("web", 2)
    live.deploy("web", deployed)
    assert live.image_of("web") == live.image_id(deployed)
    factory = create_session_factory(engine)
    EnvironmentService(UnitOfWork(factory)).update(
        project,
        DEV,
        EnvironmentUpdate(
            compose_project=live.name,
            compose_files=[str(live.compose_file)],
            compose_env_files=[str(live.env_file)],
        ),
    )
    settings = make_settings(live.directory, socket_path=socket_dir / "w.sock").model_copy(
        update={"data_dir": live.directory, "docker_host": "", "container_verify_timeout": 40.0}
    )
    services = build_container_parts(settings, factory, EnvironmentLocks()).services
    worker_before = live.container("worker")
    web_id = live.container("web")

    result = services.env.update(project, DEV, web_id, EnvChange(set={"EXTRA": "kept"}), ACTOR)

    assert result.restarted and result.containers[0].state == "running"
    assert live.image_of("web") == live.image_id(deployed), "env edit reverted the deployment"
    env = _env_of(live, "web")
    assert env["EXTRA"] == "kept" and env["PORT"] == "8080"  # override + env-file interpolation
    assert live.container("worker") == worker_before
    # limits go through the same path and keep the image as well
    services.limits.update(
        project, DEV, live.container("web"), LimitChange(cpus=0.5, memory="64m"), ACTOR
    )
    services.env.update(project, DEV, live.container("web"), EnvChange(set={"B": "2"}), ACTOR)
    assert live.image_of("web") == live.image_id(deployed)
    assert live.inspect("web", "{{.HostConfig.Memory}}") == str(64 * 1024**2)
    # a later deployment still works with both override files in place
    third = live.build("web", 3)
    wrapped = LockedDeployer(
        live.deployer(), EnvironmentLocks(), managed_files(settings.data_dir / "overrides")
    )
    wrapped.deploy(live.request("web", third), lambda _line: None, NotCancelled())
    assert live.image_of("web") == live.image_id(third)
    assert _env_of(live, "web")["EXTRA"] == "kept"
