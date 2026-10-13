"""Worker process entrypoint."""

from __future__ import annotations

import logging
import signal
import threading
from collections.abc import Mapping
from types import FrameType

from shiftcodes.config import Settings, get_settings
from shiftcodes.config_ingress import get_ingress_settings
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
from shiftcodes.worker.git_wiring import build_branch_files, build_heads
from shiftcodes.worker.github_handlers import github_handlers, project_source
from shiftcodes.worker.handlers import build_handlers
from shiftcodes.worker.ingress_handlers import build_ingress_handlers
from shiftcodes.worker.ingress_wiring import IngressFactory
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
    extra = {**github, **pipelines.handlers(), **(extra_handlers or {})}
    handlers = build_handlers(secret_service, extra)
    return RpcServer(settings.worker_socket_path, handlers)


def build_ingress(
    settings: Settings, session_factory: SessionFactory, redactor: SecretRedactor
) -> IngressFactory | None:
    """Create the ingress wiring when ``SHIFTCODES_INGRESS_ENABLED`` is set."""
    ingress_settings = get_ingress_settings()
    if not ingress_settings.enabled:
        return None
    cipher = SecretCipher(settings.secret_key_file)
    return IngressFactory(settings, ingress_settings, session_factory, cipher, redactor)


def build_scheduler(
    settings: Settings, session_factory: SessionFactory, redactor: SecretRedactor
) -> RunScheduler:
    """Create the run queue; it shares the key file with the RPC server (same process)."""
    cipher = SecretCipher(settings.secret_key_file)
    return RunScheduler(build_execution_env(settings, session_factory, cipher, redactor))


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


def main() -> None:
    """Configure logging, recover interrupted runs, serve RPC and run the queue until SIGTERM."""
    apply_shared_umask()
    settings = get_settings()
    redactor = configure_logging(settings)
    engine = create_db_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    ingress = build_ingress(settings, session_factory, redactor)
    extra = (
        build_ingress_handlers(ingress.ingress_service, ingress.check_service) if ingress else None
    )
    server = build_rpc_server(settings, session_factory, redactor, extra)
    scheduler = build_scheduler(settings, session_factory, redactor)
    shutdown = ShutdownHandler()
    shutdown.install()
    server.start()
    recover_interrupted_runs(scheduler.env)
    renewal = _start_renewal(ingress, shutdown)
    logger.info("worker started")
    try:
        scheduler.run_forever(shutdown)
    finally:
        if renewal is not None:
            renewal.join(timeout=5)
        server.stop()
        if ingress is not None:
            ingress.close()
        engine.dispose()
    logger.info("worker stopped")


if __name__ == "__main__":
    main()
