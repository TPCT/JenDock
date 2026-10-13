"""Builds container management and the terminal bridge of the worker from settings."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from backend.adapters.compose_cli import DockerComposeEngine
from backend.adapters.compose_deployer import DEPLOYMENTS_DIR_NAME
from backend.adapters.compose_overrides import ComposeOverrideStore
from backend.adapters.container_admin import DockerContainerEngine
from backend.adapters.docker_client import DockerClient
from backend.adapters.docker_commands import DockerCommands
from backend.adapters.override_store import OverrideStore
from backend.adapters.resource_overrides import ResourceOverrideStore
from backend.config import Settings
from backend.containers.base import ContainerAuditor, Membership, TargetResolver
from backend.containers.config import ContainerConfig
from backend.containers.confirmations import ConfirmationStore
from backend.containers.environment import EnvironmentEditor
from backend.containers.files import FileBrowser
from backend.containers.lifecycle import ContainerInventory, LifecycleService
from backend.containers.limits import LimitsService
from backend.containers.locks import EnvironmentLocks
from backend.containers.logs import LogService
from backend.containers.recreate import (
    CombinedOverrideFiles,
    DeployOverrideFiles,
    ServiceRecreator,
    StoreOverrideFiles,
)
from backend.containers.resource_store import ResourceLimitStore
from backend.containers.resources import (
    HostCapacityReader,
    ResourceOverrideFiles,
    ResourceService,
)
from backend.containers.telemetry import HostSampler, TelemetryCollector, TelemetryService
from backend.repositories.database import SessionFactory, UnitOfWork
from backend.terminal.opener import AuditTerminalListener, DockerExecOpener
from backend.terminal.server import TerminalServer
from backend.terminal.session import TerminalLimits, TerminalManager
from backend.worker.container_handlers import ContainerServices

OVERRIDES_DIR_NAME = "overrides"


@dataclass(frozen=True, slots=True)
class ContainerWorkerParts:
    """What the worker process starts and stops besides the RPC server."""

    services: ContainerServices
    collector: TelemetryCollector
    terminals: TerminalManager
    terminal_server: TerminalServer


def build_container_parts(
    settings: Settings,
    session_factory: SessionFactory,
    locks: EnvironmentLocks,
) -> ContainerWorkerParts:
    """Wire the docker engines, services, telemetry collector and terminal server.

    The deployer's image override file is appended after this module's override file.
    """
    client = DockerClient(settings.docker_binary, settings.docker_host)
    commands = DockerCommands(settings.docker_binary, settings.docker_host)
    engine = DockerContainerEngine(client)
    config = ContainerConfig.from_settings(settings)
    store = ComposeOverrideStore(settings.data_dir / OVERRIDES_DIR_NAME)
    limit_store = ResourceLimitStore(session_factory)
    limit_files = ResourceOverrideStore(settings.data_dir / OVERRIDES_DIR_NAME)
    # Order matters: the deployer's image override is the LAST compose file (images stay pinned).
    files = CombinedOverrideFiles(
        StoreOverrideFiles(store),
        ResourceOverrideFiles(limit_store, limit_files),
        DeployOverrideFiles(OverrideStore(settings.data_dir / DEPLOYMENTS_DIR_NAME)),
    )
    resolver = TargetResolver(session_factory)
    membership = Membership(engine)
    auditor = ContainerAuditor(session_factory)
    recreator = ServiceRecreator(engine, DockerComposeEngine(commands), files, config)

    def projects() -> Iterable[str]:
        uow = UnitOfWork(session_factory)
        with uow:
            return list(uow.environments.list_compose_projects())

    telemetry = TelemetryService(
        HostSampler(settings.data_dir),
        engine,
        resolver,
        membership,
        projects,
        settings.telemetry_history,
    )
    services = ContainerServices(
        inventory=ContainerInventory(resolver, engine, membership),
        lifecycle=LifecycleService(
            resolver, engine, membership, locks, ConfirmationStore(), auditor, config
        ),
        logs=LogService(resolver, engine, membership),
        env=EnvironmentEditor(resolver, membership, store, recreator, locks, auditor, config),
        limits=LimitsService(resolver, engine, membership, store, locks, auditor, config),
        resources=ResourceService(
            resolver,
            engine,
            limit_store,
            limit_files,
            store,
            locks,
            auditor,
            config,
            HostCapacityReader(engine),
        ),
        files=FileBrowser(resolver, engine, membership, auditor),
        telemetry=telemetry,
    )
    manager = TerminalManager(
        TerminalLimits(
            idle_seconds=settings.terminal_idle_seconds,
            max_seconds=settings.terminal_max_seconds,
            max_sessions=settings.terminal_max_sessions,
        ),
        AuditTerminalListener(auditor),
    )
    server = TerminalServer(
        settings.worker_terminal_socket_path,
        manager,
        DockerExecOpener(resolver, membership, client),
    )
    return ContainerWorkerParts(
        services, TelemetryCollector(telemetry, settings.telemetry_interval), manager, server
    )
