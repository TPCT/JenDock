"""Builds the execution environment of the worker from settings."""

from __future__ import annotations

from collections.abc import Callable

from backend.adapters.commit_status import GithubStatusPublisher
from backend.adapters.compose_deployer import DEPLOYMENTS_DIR_NAME, ComposeDeployer, DeployConfig
from backend.adapters.deploy_health import HealthPolicy
from backend.adapters.deploy_ledger import DeploymentLedger
from backend.adapters.deployer import DeployRequest
from backend.adapters.docker_cli import DockerCliRuntime
from backend.adapters.docker_commands import DockerCommands
from backend.adapters.http_probe import UrllibProbe
from backend.adapters.resource_overrides import ResourceOverrideStore
from backend.adapters.workspace import WorkspaceManager
from backend.config import Settings
from backend.containers.base import ContainerAuditor, TargetResolver
from backend.containers.locks import EnvironmentLocks, LockedDeployer, managed_files
from backend.containers.resource_store import ResourceLimitStore
from backend.containers.resources import ResourceOverrideFiles
from backend.domain.actor import Actor
from backend.domain.containers import EnvironmentKey
from backend.domain.enums import EnvironmentName
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
from backend.services.notification_events import NoRunNotifications, NotifyingLedger
from backend.services.secret_cipher import SecretCipher
from backend.services.secrets import SecretService
from backend.worker.container_wiring import OVERRIDES_DIR_NAME
from backend.worker.git_wiring import build_source
from backend.worker.notification_wiring import NotificationParts

PRIVATE_DIR_NAME = "worker-private"
WORKSPACES_DIR_NAME = "workspaces"
LOGS_DIR_NAME = "logs"


def apply_development_default_limits(  # noqa: PLR0913 - collaborators keep the deploy hook testable
    request: DeployRequest,
    key: EnvironmentKey,
    resolver: TargetResolver,
    limit_store: ResourceLimitStore,
    auditor: ContainerAuditor,
    *,
    cpus: float,
    memory_mib: int,
) -> None:
    """Create and audit missing development limits before the override file is rendered."""
    if key.environment is not EnvironmentName.DEVELOPMENT:
        return
    target = resolver.resolve(key.project, key.environment)
    inserted = limit_store.apply_development_defaults(
        target.environment_id,
        [item.service for item in request.services],
        cpus,
        memory_mib * 1024 * 1024,
    )
    if inserted:
        auditor.record_environment(
            Actor.system("development-default-limits"),
            "resource_limits.default_applied",
            target,
            {"services": inserted},
        )


def build_execution_env(  # noqa: PLR0913, PLR0917 - one optional collaborator each
    settings: Settings,
    session_factory: SessionFactory,
    cipher: SecretCipher,
    redactor: SecretRedactor,
    locks: EnvironmentLocks | None = None,
    notifications: NotificationParts | None = None,
) -> ExecutionEnv:
    """Wire the docker CLI runtime, git source, workspaces, logs and secret access.

    Deployment goes through ``ComposeDeployer`` (journals and managed overrides live under
    ``<data_dir>/deployments``), wrapped so every deployment holds its environment's lock, shared
    with container operations (``locks``). The git source clones over ssh (deploy key) or https
    depending on the project; commit status reporting is off unless enabled. With
    ``notifications`` runs and deployments announce their outcome (``notification_wiring``).
    """
    data_dir = settings.data_dir

    def secret_service() -> SecretService:
        return SecretService(UnitOfWork(session_factory), cipher, redactor)

    probe = UrllibProbe()
    ledger: DeploymentLedger = DatabaseLedger(lambda: UnitOfWork(session_factory))
    if notifications is not None:
        ledger = NotifyingLedger(ledger, notifications.deployments)
    deployer = ComposeDeployer(
        DockerCommands(settings.docker_binary, settings.docker_host),
        ledger,
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

    resource_files = ResourceOverrideFiles(
        ResourceLimitStore(session_factory),
        ResourceOverrideStore(data_dir / OVERRIDES_DIR_NAME),
    )

    limit_store = ResourceLimitStore(session_factory)
    auditor = ContainerAuditor(session_factory)
    resolver = TargetResolver(session_factory)

    def apply_defaults(request: DeployRequest, key: EnvironmentKey) -> None:
        apply_development_default_limits(
            request,
            key,
            resolver,
            limit_store,
            auditor,
            cpus=settings.dev_default_cpus,
            memory_mib=settings.dev_default_memory_mib,
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
            deployer,
            locks or EnvironmentLocks(),
            managed_files(data_dir / OVERRIDES_DIR_NAME, resource_files.render, apply_defaults),
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
        notifications=notifications.runs if notifications else NoRunNotifications(),
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
