"""Builds the database inspector's RPC handlers from settings."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from backend.adapters.container_admin import DockerContainerEngine
from backend.adapters.docker_cli import DockerCliRuntime
from backend.adapters.docker_client import DockerClient
from backend.adapters.inspector_runner import InspectorRunner
from backend.adapters.worker_client import SecretsGateway
from backend.config import Settings
from backend.domain.actor import Actor
from backend.domain.enums import EnvironmentName
from backend.logging import SecretRedactor
from backend.repositories.database import SessionFactory, UnitOfWork
from backend.services.database_detection import DatabaseDetectionService, DetectionLockRegistry
from backend.services.database_inspection import InspectionService
from backend.services.secret_cipher import SecretCipher
from backend.services.secrets import SecretInfo, SecretService
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
    containers = DockerContainerEngine(DockerClient(settings.docker_binary, settings.docker_host))
    detection_locks = DetectionLockRegistry()

    def service() -> InspectionService:
        return InspectionService(
            lambda: UnitOfWork(session_factory),
            lambda: SecretService(UnitOfWork(session_factory), cipher, redactor),
            runner,
        )

    class InProcessSecrets:
        def set_secret(
            self, slug: str, env: EnvironmentName, name: str, value: str, actor: Actor
        ) -> SecretInfo:
            return SecretService(UnitOfWork(session_factory), cipher, redactor).set(
                slug, env, name, value, actor
            )

        def delete_secret(self, slug: str, env: EnvironmentName, name: str, actor: Actor) -> None:
            return SecretService(UnitOfWork(session_factory), cipher, redactor).delete(
                slug, env, name, actor
            )

        def ping(self) -> None:
            return None

        def list_secrets(
            self,
            slug: str,
            env: EnvironmentName,
            actor: Actor,  # noqa: ARG002
        ) -> Sequence[SecretInfo]:
            return SecretService(UnitOfWork(session_factory), cipher, redactor).list_names(
                slug, env
            )

        def reveal_secret(self, slug: str, env: EnvironmentName, name: str, actor: Actor) -> str:
            return SecretService(UnitOfWork(session_factory), cipher, redactor).reveal(
                slug, env, name, actor
            )

    secrets: SecretsGateway = InProcessSecrets()

    def detection() -> DatabaseDetectionService:
        return DatabaseDetectionService(
            UnitOfWork(session_factory),
            secrets,
            containers,
            service(),
            lock_registry=detection_locks,
        )

    return build_database_handlers(service, detection)
