"""Worker process entrypoint."""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Mapping
from types import FrameType

from shiftcodes.config import Settings, get_settings
from shiftcodes.containers.locks import EnvironmentLocks
from shiftcodes.execution.recovery import recover_interrupted_runs
from shiftcodes.execution.scheduler import RunScheduler
from shiftcodes.logging import SecretRedactor, configure_logging
from shiftcodes.process import apply_shared_umask
from shiftcodes.repositories.database import (
    SessionFactory,
    UnitOfWork,
    create_db_engine,
    create_session_factory,
)
from shiftcodes.services.secret_cipher import SecretCipher
from shiftcodes.services.secrets import SecretService
from shiftcodes.worker.container_handlers import build_container_handlers
from shiftcodes.worker.container_wiring import build_container_parts
from shiftcodes.worker.git_wiring import build_branch_files, build_heads
from shiftcodes.worker.github_handlers import github_handlers, project_source
from shiftcodes.worker.handlers import build_handlers
from shiftcodes.worker.pipeline_handlers import PipelineHandlers
from shiftcodes.worker.rpc import Handler, RpcServer
from shiftcodes.worker.wiring import build_execution_env

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
) -> RpcServer:
    """Create the RPC server; the worker is the only process that loads the key file."""
    cipher = SecretCipher(settings.secret_key_file)

    def secret_service() -> SecretService:
        return SecretService(UnitOfWork(session_factory), cipher, redactor)

    github = github_handlers(
        settings.github_webhook_secret_file,
        build_heads(settings),
        project_source(lambda: UnitOfWork(session_factory)),
    )
    pipelines = PipelineHandlers(lambda: UnitOfWork(session_factory), build_branch_files(settings))
    handlers = build_handlers(
        secret_service, {**github, **pipelines.handlers(), **(extra_handlers or {})}
    )
    return RpcServer(settings.worker_socket_path, handlers)


def build_scheduler(
    settings: Settings,
    session_factory: SessionFactory,
    redactor: SecretRedactor,
    locks: EnvironmentLocks | None = None,
) -> RunScheduler:
    """Create the run queue; it shares the key file with the RPC server (same process)."""
    cipher = SecretCipher(settings.secret_key_file)
    return RunScheduler(build_execution_env(settings, session_factory, cipher, redactor, locks))


def main() -> None:
    """Configure logging, recover interrupted runs, serve RPC and run the queue until SIGTERM."""
    apply_shared_umask()
    settings = get_settings()
    redactor = configure_logging(settings)
    engine = create_db_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    locks = EnvironmentLocks()  # shared by deployments and container operations
    containers = build_container_parts(settings, session_factory, locks)
    server = build_rpc_server(
        settings, session_factory, redactor, build_container_handlers(containers.services)
    )
    scheduler = build_scheduler(settings, session_factory, redactor, locks)
    shutdown = ShutdownHandler()
    shutdown.install()
    server.start()
    containers.terminals.start()
    containers.terminal_server.start()
    containers.collector.start()
    recover_interrupted_runs(scheduler.env)
    logger.info("worker started")
    try:
        scheduler.run_forever(shutdown)
    finally:
        containers.collector.stop()
        containers.terminal_server.stop()
        containers.terminals.shutdown()
        server.stop()
        engine.dispose()
    logger.info("worker stopped")


if __name__ == "__main__":
    main()
