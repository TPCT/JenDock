"""Builds the database inspector's RPC handlers from settings."""

from __future__ import annotations

from collections.abc import Mapping

from shiftcodes.adapters.docker_cli import DockerCliRuntime
from shiftcodes.adapters.inspector_runner import InspectorRunner
from shiftcodes.config import Settings
from shiftcodes.logging import SecretRedactor
from shiftcodes.repositories.database import SessionFactory, UnitOfWork
from shiftcodes.services.database_inspection import InspectionService
from shiftcodes.services.secret_cipher import SecretCipher
from shiftcodes.services.secrets import SecretService
from shiftcodes.worker.database_handlers import build_database_handlers
from shiftcodes.worker.rpc import Handler

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
