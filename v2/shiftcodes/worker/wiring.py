"""Builds the execution environment of the worker from settings."""

from __future__ import annotations

from shiftcodes.adapters.deployer import UnavailableDeployer
from shiftcodes.adapters.docker_cli import DockerCliRuntime
from shiftcodes.adapters.http_probe import UrllibProbe
from shiftcodes.adapters.repo_files import GitFileReader
from shiftcodes.adapters.sources import LocalGitSource
from shiftcodes.adapters.workspace import WorkspaceManager
from shiftcodes.config import Settings
from shiftcodes.execution.config import ExecutionConfig
from shiftcodes.execution.environment import ExecutionEnv
from shiftcodes.execution.logs import LogStore
from shiftcodes.logging import SecretRedactor
from shiftcodes.repositories.database import SessionFactory, UnitOfWork
from shiftcodes.services.secret_cipher import SecretCipher
from shiftcodes.services.secrets import SecretService

PRIVATE_DIR_NAME = "worker-private"
WORKSPACES_DIR_NAME = "workspaces"
LOGS_DIR_NAME = "logs"


def build_repository_reader(settings: Settings) -> GitFileReader:
    """Reads single files of a repository branch, from the same place runs clone from."""
    template = settings.git_url_template
    return GitFileReader(lambda repo: template.format(repository=repo), settings.git_binary)


def build_execution_env(
    settings: Settings,
    session_factory: SessionFactory,
    cipher: SecretCipher,
    redactor: SecretRedactor,
) -> ExecutionEnv:
    """Wire the docker CLI runtime, git source, workspaces, logs and secret access.

    Deployment is a placeholder until Task 7; the git source clones from
    ``settings.git_url_template`` until Task 6 adds deploy keys.
    """
    data_dir = settings.data_dir
    template = settings.git_url_template

    def secret_service() -> SecretService:
        return SecretService(UnitOfWork(session_factory), cipher, redactor)

    return ExecutionEnv(
        config=ExecutionConfig.from_settings(settings),
        runtime=DockerCliRuntime(
            binary=settings.docker_binary,
            docker_host=settings.docker_host,
            private_dir=data_dir / PRIVATE_DIR_NAME,
        ),
        source=LocalGitSource(lambda repo: template.format(repository=repo), settings.git_binary),
        deployer=UnavailableDeployer(),
        probe=UrllibProbe(),
        workspaces=WorkspaceManager(
            data_dir / WORKSPACES_DIR_NAME,
            settings.workspace_max_bytes,
            settings.workspace_host_dir,
        ),
        logs=LogStore(data_dir / LOGS_DIR_NAME),
        session_factory=session_factory,
        secret_service=secret_service,
    )
