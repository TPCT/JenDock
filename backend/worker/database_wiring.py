"""Builds the database inspector's RPC handlers from settings."""

from __future__ import annotations

from collections.abc import Mapping

from backend.adapters.docker_cli import DockerCliRuntime
from backend.adapters.inspector_runner import InspectorRunner
from backend.config import Settings
from backend.logging import SecretRedactor
from backend.repositories.database import SessionFactory, UnitOfWork
from backend.services.database_inspection import InspectionService
from backend.services.secret_cipher import SecretCipher
from backend.services.secrets import SecretService
from backend.worker.database_handlers import build_database_handlers
from backend.worker.rpc import Handler

PRIVATE_DIR_NAME = "worker-private"


def build_inspection_handlers(
    settings: Settings,
    session_factory: SessionFactory,
    cipher: SecretCipher,
    redactor: SecretRedactor,
) -> Mapping[str, Handler]:
    """Wire the helper-container runner and the inspection service into ``db.*`` handlers."""
    runtime = DockerCliRuntime(
        binary=settings.docker_binary,
        docker_host=settings.docker_host,
        private_dir=settings.data_dir / PRIVATE_DIR_NAME,
    )
    runner = InspectorRunner(runtime, settings.inspector_image)

    def service() -> InspectionService:
        return InspectionService(
            lambda: UnitOfWork(session_factory),
            lambda: SecretService(UnitOfWork(session_factory), cipher, redactor),
            runner,
        )

    return build_database_handlers(service)
