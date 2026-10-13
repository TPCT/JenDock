"""Builds the execution environment of the worker from settings."""

from __future__ import annotations

from collections.abc import Callable

from backend.adapters.commit_status import GithubStatusPublisher
from backend.adapters.compose_deployer import DEPLOYMENTS_DIR_NAME, ComposeDeployer, DeployConfig
from backend.adapters.deploy_health import HealthPolicy
from backend.adapters.docker_cli import DockerCliRuntime
from backend.adapters.docker_commands import DockerCommands
from backend.adapters.http_probe import UrllibProbe
from backend.adapters.workspace import WorkspaceManager
from backend.config import Settings
from backend.containers.locks import EnvironmentLocks, LockedDeployer, managed_files
from backend.execution.config import ExecutionConfig
from backend.execution.environment import ExecutionEnv
from backend.execution.logs import LogStore
from backend.execution.status_reporter import (
    CommitStatusReporter,
    NoStatusReporter,
    StatusReporter,
)
from backend.logging import SecretRedactor
from backend.repositories.database import SessionFactory, UnitOfWork
from backend.services.deployments import DatabaseLedger
from backend.services.secret_cipher import SecretCipher
from backend.services.secrets import SecretService
from backend.worker.container_wiring import OVERRIDES_DIR_NAME
from backend.worker.git_wiring import build_source

PRIVATE_DIR_NAME = "worker-private"
WORKSPACES_DIR_NAME = "workspaces"
LOGS_DIR_NAME = "logs"


def build_execution_env(
    settings: Settings,
    session_factory: SessionFactory,
    cipher: SecretCipher,
    redactor: SecretRedactor,
    locks: EnvironmentLocks | None = None,
) -> ExecutionEnv:
    """Wire the docker CLI runtime, git source, workspaces, logs and secret access.

    Deployment goes through ``ComposeDeployer`` (journals and managed overrides live under
    ``<data_dir>/deployments``), wrapped so every deployment holds its environment's lock, shared
    with container operations (``locks``). The git source clones over ssh (deploy key) or https
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
        deployer=LockedDeployer(
            deployer, locks or EnvironmentLocks(), managed_files(data_dir / OVERRIDES_DIR_NAME)
        ),
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
