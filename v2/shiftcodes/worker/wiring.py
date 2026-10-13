"""Builds the execution environment of the worker from settings."""

from __future__ import annotations

from collections.abc import Callable

from shiftcodes.adapters.commit_status import GithubStatusPublisher
from shiftcodes.adapters.compose_deployer import DEPLOYMENTS_DIR_NAME, ComposeDeployer, DeployConfig
from shiftcodes.adapters.deploy_health import HealthPolicy
from shiftcodes.adapters.docker_cli import DockerCliRuntime
from shiftcodes.adapters.docker_commands import DockerCommands
from shiftcodes.adapters.http_probe import UrllibProbe
from shiftcodes.adapters.workspace import WorkspaceManager
from shiftcodes.config import Settings
from shiftcodes.execution.config import ExecutionConfig
from shiftcodes.execution.environment import ExecutionEnv
from shiftcodes.execution.logs import LogStore
from shiftcodes.execution.status_reporter import (
    CommitStatusReporter,
    NoStatusReporter,
    StatusReporter,
)
from shiftcodes.logging import SecretRedactor
from shiftcodes.repositories.database import SessionFactory, UnitOfWork
from shiftcodes.services.deployments import DatabaseLedger
from shiftcodes.services.secret_cipher import SecretCipher
from shiftcodes.services.secrets import SecretService
from shiftcodes.worker.git_wiring import build_source

PRIVATE_DIR_NAME = "worker-private"
WORKSPACES_DIR_NAME = "workspaces"
LOGS_DIR_NAME = "logs"


def build_execution_env(
    settings: Settings,
    session_factory: SessionFactory,
    cipher: SecretCipher,
    redactor: SecretRedactor,
) -> ExecutionEnv:
    """Wire the docker CLI runtime, git source, workspaces, logs and secret access.

    Deployment goes through ``ComposeDeployer`` (journals and managed overrides live under
    ``<data_dir>/deployments``). The git source clones over ssh (deploy key) or https
    depending on the project; commit status reporting is off unless enabled.
    """
    data_dir = settings.data_dir

    def secret_service() -> SecretService:
        return SecretService(UnitOfWork(session_factory), cipher, redactor)

    probe = UrllibProbe()
    deployer = ComposeDeployer(
        DockerCommands(settings.docker_binary, settings.docker_host),
        DatabaseLedger(lambda: UnitOfWork(session_factory)),
        data_dir / DEPLOYMENTS_DIR_NAME,
        probe,
        config=DeployConfig(
            health=HealthPolicy(
                timeout=float(settings.deploy_health_timeout),
                settle=settings.deploy_settle_seconds,
            ),
            keep_images=settings.image_retention,
        ),
    )

    return ExecutionEnv(
        config=ExecutionConfig.from_settings(settings),
        runtime=DockerCliRuntime(
            binary=settings.docker_binary,
            docker_host=settings.docker_host,
            private_dir=data_dir / PRIVATE_DIR_NAME,
        ),
        source=build_source(settings),
        deployer=deployer,
        probe=probe,
        workspaces=WorkspaceManager(
            data_dir / WORKSPACES_DIR_NAME,
            settings.workspace_max_bytes,
            settings.workspace_host_dir,
        ),
        logs=LogStore(data_dir / LOGS_DIR_NAME),
        session_factory=session_factory,
        secret_service=secret_service,
        status=_status_reporter(settings, secret_service),
    )


def _status_reporter(
    settings: Settings, secret_service: Callable[[], SecretService]
) -> StatusReporter:
    if not settings.github_status_enabled:
        return NoStatusReporter()
    return CommitStatusReporter(
        GithubStatusPublisher(),
        secret_service,
        settings.github_status_token_secret,
        settings.public_origin,
    )
