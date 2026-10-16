"""Worker process entrypoint."""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Mapping
from types import FrameType

from backend.backup.schedule import BackupScheduler
from backend.config import Settings, get_settings
from backend.config_ingress import get_ingress_settings
from backend.containers.locks import EnvironmentLocks
from backend.execution.recovery import recover_interrupted_runs
from backend.execution.scheduler import RunScheduler
from backend.logging import SecretRedactor, configure_logging
from backend.process import apply_shared_umask
from backend.repositories.database import (
    SessionFactory,
    UnitOfWork,
    create_db_engine,
    create_session_factory,
)
from backend.services.auto_stop import AutoStopScheduler
from backend.services.database_backups import DatabaseBackupService
from backend.services.secret_cipher import SecretCipher
from backend.services.secrets import SecretService
from backend.services.totp import TotpVault
from backend.worker.container_handlers import build_container_handlers
from backend.worker.container_wiring import ContainerWorkerParts, build_container_parts
from backend.worker.database_backup_wiring import build_database_backups
from backend.worker.database_wiring import build_inspection_handlers
from backend.worker.discovery_wiring import build_discovery
from backend.worker.git_wiring import build_branch_files, build_heads
from backend.worker.github_handlers import github_handlers, project_source
from backend.worker.handlers import build_handlers
from backend.worker.ingress_handlers import build_ingress_handlers
from backend.worker.ingress_wiring import IngressFactory
from backend.worker.integrations_wiring import build_integrations
from backend.worker.notification_wiring import NotificationParts, build_notification_parts
from backend.worker.pipeline_handlers import PipelineHandlers
from backend.worker.rpc import Handler, RpcServer
from backend.worker.totp_handlers import totp_handlers
from backend.worker.wiring import build_execution_env

logger = logging.getLogger(__name__)


class ShutdownHandler:
    """Signal handler that flips a stop event so loops can exit gracefully."""

    def __init__(self) -> None:
        self._stop = threading.Event()

    @property
    def requested(self) -> bool:
        """Whether shutdown has been requested."""
        return self._stop.is_set()

    def handle(self, signum: int, _frame: FrameType | None) -> None:
        """Record the shutdown request."""
        logger.info("shutdown requested", extra={"signal": signum})
        self._stop.set()

    def wait(self, timeout: float) -> bool:
        """Sleep up to ``timeout`` seconds; return True if shutdown was requested."""
        return self._stop.wait(timeout)

    def install(self) -> None:
        """Register for SIGTERM and SIGINT."""
        signal.signal(signal.SIGTERM, self.handle)
        signal.signal(signal.SIGINT, self.handle)


def build_rpc_server(
    settings: Settings,
    session_factory: SessionFactory,
    redactor: SecretRedactor,
    extra_handlers: Mapping[str, Handler] | None = None,
    notifications: NotificationParts | None = None,
) -> RpcServer:
    """Create the RPC server; the worker is the only process that loads the key file."""
    cipher = SecretCipher(settings.secret_key_file)

    def secret_service() -> SecretService:
        return SecretService(UnitOfWork(session_factory), cipher, redactor)

    github = github_handlers(
        settings.github_webhook_secret_file,
        build_heads(settings),
        project_source(lambda: UnitOfWork(session_factory)),
        notifications.webhook.record_failure if notifications else None,
    )
    pipelines = PipelineHandlers(lambda: UnitOfWork(session_factory), build_branch_files(settings))
    inspection = build_inspection_handlers(settings, session_factory, cipher, redactor)
    integrations = build_integrations(
        settings, get_ingress_settings(), session_factory, cipher, redactor
    )
    channels = notifications.handlers if notifications else {}
    two_factor = totp_handlers(TotpVault(cipher))
    extra = {
        **github,
        **pipelines.handlers(),
        **inspection,
        **integrations,
        **two_factor,
        **channels,
        **(extra_handlers or {}),
    }
    handlers = build_handlers(secret_service, extra)
    return RpcServer(settings.worker_socket_path, handlers)


def build_ingress(
    settings: Settings,
    session_factory: SessionFactory,
    redactor: SecretRedactor,
    notifications: NotificationParts | None = None,
) -> IngressFactory | None:
    """Create the ingress wiring when ``JENDOCK_INGRESS_ENABLED`` is set."""
    ingress_settings = get_ingress_settings()
    if not ingress_settings.enabled:
        return None
    cipher = SecretCipher(settings.secret_key_file)
    notifier = notifications.certificates if notifications else None
    return IngressFactory(
        settings, ingress_settings, session_factory, cipher, redactor, notifier=notifier
    )


def build_scheduler(
    settings: Settings,
    session_factory: SessionFactory,
    redactor: SecretRedactor,
    locks: EnvironmentLocks | None = None,
    notifications: NotificationParts | None = None,
) -> RunScheduler:
    """Create the run queue; it shares the key file with the RPC server (same process)."""
    cipher = SecretCipher(settings.secret_key_file)
    env = build_execution_env(settings, session_factory, cipher, redactor, locks, notifications)
    return RunScheduler(env)


def _start_renewal(
    ingress: IngressFactory | None, shutdown: ShutdownHandler
) -> threading.Thread | None:
    """Run the certificate renewal loop on its own thread when ingress is enabled."""
    if ingress is None:
        return None
    thread = threading.Thread(
        target=ingress.renewal_scheduler().run_forever,
        args=(shutdown,),
        name="certificate-renewal",
        daemon=True,
    )
    thread.start()
    return thread


def _start_backups(settings: Settings, shutdown: ShutdownHandler) -> threading.Thread | None:
    """Run the daily backup loop on its own thread when ``JENDOCK_BACKUP_ENABLED`` is set."""
    if not settings.backup_enabled:
        return None
    thread = threading.Thread(
        target=BackupScheduler(settings).run_forever,
        args=(shutdown,),
        name="backup-schedule",
        daemon=True,
    )
    thread.start()
    return thread


def _start_database_backups(handlers: object, shutdown: ShutdownHandler) -> threading.Thread:
    thread = threading.Thread(
        target=handlers.schedule_forever,  # type: ignore[attr-defined]
        args=(shutdown,),
        name="database-backup-schedule",
        daemon=True,
    )
    thread.start()
    return thread


def _start_optional_schedulers(
    settings: Settings,
    containers: ContainerWorkerParts,
    session_factory: SessionFactory,
    database_backup_scheduler: object,
    shutdown: ShutdownHandler,
) -> tuple[threading.Thread | None, threading.Thread | None, threading.Thread]:
    backups = _start_backups(settings, shutdown)
    auto_stop = None
    services = containers.services
    if services.power is not None:
        auto_stop = threading.Thread(
            target=AutoStopScheduler(
                session_factory,
                services.power,
                settings.auto_stop_interval,
            ).run_forever,
            args=(shutdown,),
            name="environment-auto-stop",
            daemon=True,
        )
        auto_stop.start()
    database_backups = _start_database_backups(database_backup_scheduler, shutdown)
    return backups, auto_stop, database_backups


def main() -> None:
    """Configure logging, recover interrupted runs, serve RPC and run the queue until SIGTERM."""
    apply_shared_umask()
    settings = get_settings()
    redactor = configure_logging(settings)
    engine = create_db_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    locks = EnvironmentLocks()  # shared by deployments and container operations
    containers = build_container_parts(settings, session_factory, locks)
    notifications = build_notification_parts(
        settings, session_factory, SecretCipher(settings.secret_key_file), redactor
    )
    database_backup_handlers, database_backup_scheduler = build_database_backups(
        settings,
        session_factory,
        SecretCipher(settings.secret_key_file),
        redactor,
        notifications.sink,
    )
    ingress = build_ingress(settings, session_factory, redactor, notifications)
    discovery = build_discovery(
        settings,
        get_ingress_settings(),
        session_factory,
        SecretCipher(settings.secret_key_file),
        redactor,
        containers.services.inventory,
    )
    extra = {
        **build_container_handlers(containers.services),
        **discovery,
        **database_backup_handlers,
        **(
            build_ingress_handlers(ingress.ingress_service, ingress.check_service)
            if ingress
            else {}
        ),
    }
    server = build_rpc_server(settings, session_factory, redactor, extra, notifications)
    scheduler = build_scheduler(settings, session_factory, redactor, locks, notifications)
    shutdown = ShutdownHandler()
    shutdown.install()
    server.start()
    containers.terminals.start()
    containers.terminal_server.start()
    containers.collector.start()
    recover_interrupted_runs(scheduler.env)
    DatabaseBackupService(UnitOfWork(session_factory)).fail_interrupted()
    renewal = _start_renewal(ingress, shutdown)
    backups, auto_stop, database_backups = _start_optional_schedulers(
        settings, containers, session_factory, database_backup_scheduler, shutdown
    )
    notifiers = notifications.start(shutdown)
    logger.info("worker started")
    try:
        scheduler.run_forever(shutdown)
    finally:
        containers.collector.stop()
        containers.terminal_server.stop()
        containers.terminals.shutdown()
        for thread in notifiers:
            thread.join(timeout=5)
        if backups is not None:
            backups.join(timeout=5)
        if auto_stop is not None:
            auto_stop.join(timeout=5)
        database_backups.join(timeout=5)
        if renewal is not None:
            renewal.join(timeout=5)
        server.stop()
        if ingress is not None:
            ingress.close()
        engine.dispose()
    logger.info("worker stopped")


if __name__ == "__main__":
    main()
