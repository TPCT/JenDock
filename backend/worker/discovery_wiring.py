"""Builds the worker's domain discovery from settings: Cloudflare, the host helper, containers.

Services own a non-reentrant unit of work, so the factory creates fresh ones per call. The
Cloudflare token is the platform secret from Settings -> Integrations, read only while a call
needs it. Discovery does not depend on ``JENDOCK_INGRESS_ENABLED``: it reads only.
"""

from __future__ import annotations

from collections.abc import Mapping

import httpx

from backend.adapters.cloudflare import CloudflareClient
from backend.adapters.host_helper_client import HostHelperClient
from backend.adapters.probes import StdlibHeadProber
from backend.config import Settings
from backend.config_ingress import IngressSettings
from backend.containers.lifecycle import ContainerInventory
from backend.domain.discovery import ProjectRef, PublishedPort
from backend.domain.enums import EnvironmentName
from backend.domain.errors import DomainError
from backend.logging import SecretRedactor
from backend.repositories.database import SessionFactory, UnitOfWork
from backend.services.adopted_checks import AdoptedCheckService
from backend.services.discovery import DiscoveryPorts, DiscoveryService, resolve_addresses
from backend.services.platform_secrets import CLOUDFLARE_TOKEN, PlatformSecretService
from backend.services.platform_settings import PlatformSettingsService
from backend.services.secret_cipher import SecretCipher
from backend.worker.discovery_handlers import build_discovery_handlers
from backend.worker.rpc import Handler

HTTP_TIMEOUT = 20.0
HELPER_TIMEOUT = 15.0


class WorkerPortCatalog:
    """Projects from the database and published ports from the container inventory."""

    def __init__(self, session_factory: SessionFactory, inventory: ContainerInventory) -> None:
        self._session_factory = session_factory
        self._inventory = inventory

    def _environments(self) -> list[tuple[str, str, EnvironmentName, bool]]:
        uow = UnitOfWork(self._session_factory)
        found: list[tuple[str, str, EnvironmentName, bool]] = []
        with uow:
            for project in uow.projects.list():
                for env in uow.environments.list_for_project(project.id):
                    found.append((project.slug, project.name, env.name, bool(env.compose_project)))
        return found

    def projects(self) -> list[ProjectRef]:
        """Every active project with its environments."""
        grouped: dict[str, tuple[str, list[EnvironmentName]]] = {}
        for slug, name, env, _composed in self._environments():
            grouped.setdefault(slug, (name, []))[1].append(env)
        return [ProjectRef(slug, name, tuple(envs)) for slug, (name, envs) in grouped.items()]

    def published_ports(self) -> list[PublishedPort]:
        """The host ports published by every project's containers (unreachable ones skipped)."""
        found: list[PublishedPort] = []
        for slug, _name, env, composed in self._environments():
            if not composed:
                continue
            try:
                containers = self._inventory.list(slug, env)
            except DomainError:
                continue  # one broken environment must not hide the others
            for container in containers:
                for binding in container.ports:
                    port = _published(slug, env, container.service, binding)
                    if port is not None:
                        found.append(port)
        return found


def _published(
    slug: str, env: EnvironmentName, service: str | None, binding: object
) -> PublishedPort | None:
    container_port = getattr(binding, "container_port", "")
    host_port = getattr(binding, "host_port", None)
    if service is None or not host_port:
        return None
    try:
        return PublishedPort(
            slug, env, service, int(str(container_port).split("/", 1)[0]), int(host_port)
        )
    except ValueError:
        return None


def build_discovery(  # noqa: PLR0913, PLR0917 - one collaborator each
    settings: Settings,
    ingress: IngressSettings,
    session_factory: SessionFactory,
    cipher: SecretCipher,
    redactor: SecretRedactor,
    inventory: ContainerInventory,
    transport: httpx.BaseTransport | None = None,
) -> Mapping[str, Handler]:
    """The ``discovery.*`` methods; registered whether or not ingress is enabled."""
    http = httpx.Client(timeout=HTTP_TIMEOUT, transport=transport)

    def token() -> str | None:
        secrets = PlatformSecretService(UnitOfWork(session_factory), cipher, redactor)
        return secrets.resolve(CLOUDFLARE_TOKEN)

    def catalog(value: str) -> CloudflareClient:
        return CloudflareClient(value, base_url=ingress.cloudflare_api_base, client=http)

    def effective() -> IngressSettings:
        return PlatformSettingsService(UnitOfWork(session_factory)).effective(ingress)

    ports = DiscoveryPorts(
        token=token,
        catalog=catalog,
        helper=HostHelperClient(ingress.helper_socket_path, timeout=HELPER_TIMEOUT),
        ports=WorkerPortCatalog(session_factory, inventory),
        settings=effective,
        resolve=resolve_addresses,
        public_origin=settings.public_origin,
    )

    def discovery() -> DiscoveryService:
        return DiscoveryService(UnitOfWork(session_factory), ports)

    def checks() -> AdoptedCheckService:
        return AdoptedCheckService(UnitOfWork(session_factory), ports, StdlibHeadProber())

    return build_discovery_handlers(discovery, checks)
