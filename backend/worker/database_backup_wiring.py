"""Wire the database backup runner, RPC methods and scheduler."""

from __future__ import annotations

from collections.abc import Mapping

from backend.adapters.docker_cli import DockerCliRuntime
from backend.config import Settings
from backend.logging import SecretRedactor
from backend.repositories.database import SessionFactory, UnitOfWork
from backend.services.notification_outbox import EventSink, NoEvents
from backend.services.platform_secrets import PlatformSecretService
from backend.services.secret_cipher import SecretCipher
from backend.services.secrets import SecretService
from backend.worker.database_backup_handlers import (
    BackupLocks,
    DatabaseBackupHandlers,
    build_database_backup_handlers,
)
from backend.worker.rpc import Handler


def build_database_backups(
    settings: Settings,
    session_factory: SessionFactory,
    cipher: SecretCipher,
    redactor: SecretRedactor,
    sink: EventSink | None,
) -> tuple[Mapping[str, Handler], DatabaseBackupHandlers]:
    runtime = DockerCliRuntime(
        binary=settings.docker_binary,
        docker_host=settings.docker_host,
        private_dir=settings.data_dir / "worker-private",
    )
    locks = BackupLocks()
    handlers = DatabaseBackupHandlers(
        lambda: UnitOfWork(session_factory),
        runtime,
        lambda: PlatformSecretService(UnitOfWork(session_factory), cipher, redactor),
        lambda: SecretService(UnitOfWork(session_factory), cipher, redactor),
        (sink or NoEvents()).emit,
        locks,
    )
    return build_database_backup_handlers(handlers), handlers
