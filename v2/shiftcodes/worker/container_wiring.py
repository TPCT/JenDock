"""Builds container management and the terminal bridge of the worker from settings."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from shiftcodes.adapters.compose_cli import DockerComposeEngine
from shiftcodes.adapters.compose_overrides import ComposeOverrideStore
from shiftcodes.adapters.container_admin import DockerContainerEngine
from shiftcodes.adapters.docker_client import DockerClient
from shiftcodes.config import Settings
from shiftcodes.containers.base import ContainerAuditor, Membership, TargetResolver
from shiftcodes.containers.config import ContainerConfig
from shiftcodes.containers.confirmations import ConfirmationStore
from shiftcodes.containers.environment import EnvironmentEditor
from shiftcodes.containers.files import FileBrowser
from shiftcodes.containers.lifecycle import ContainerInventory, LifecycleService
from shiftcodes.containers.limits import LimitsService
from shiftcodes.containers.locks import EnvironmentLocks
from shiftcodes.containers.logs import LogService
from shiftcodes.containers.recreate import (
    CombinedOverrideFiles,
    OverrideFiles,
    ServiceRecreator,
    StoreOverrideFiles,
)
from shiftcodes.containers.telemetry import HostSampler, TelemetryCollector, TelemetryService
from shiftcodes.repositories.database import SessionFactory, UnitOfWork
from shiftcodes.terminal.opener import AuditTerminalListener, DockerExecOpener
from shiftcodes.terminal.server import TerminalServer
from shiftcodes.terminal.session import TerminalLimits, TerminalManager
from shiftcodes.worker.container_handlers import ContainerServices

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
    other_override_files: Sequence[OverrideFiles] = (),
) -> ContainerWorkerParts:
    """Wire the docker engines, services, telemetry collector and terminal server.

    ``other_override_files`` lets the deploy engine contribute its own override files (the
    image override) to the compose invocations of container operations. They come AFTER this
    module's environment/limits override, so the deployed images stay pinned (the deploy
    engine's file must be the last ``-f``).
    """
    client = DockerClient(settings.docker_binary, settings.docker_host)
    engine = DockerContainerEngine(client)
    config = ContainerConfig.from_settings(settings)
    store = ComposeOverrideStore(settings.data_dir / OVERRIDES_DIR_NAME)
    files = CombinedOverrideFiles(StoreOverrideFiles(store), *other_override_files)
    resolver = TargetResolver(session_factory)
    membership = Membership(engine)
    auditor = ContainerAuditor(session_factory)
    recreator = ServiceRecreator(engine, DockerComposeEngine(client), files, config)

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
