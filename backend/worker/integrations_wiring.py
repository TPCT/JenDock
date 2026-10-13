"""Builds the Integrations RPC handlers (Cloudflare credential, deploy keys) from settings."""

from __future__ import annotations

from collections.abc import Mapping

import httpx

from backend.adapters.cloudflare import CloudflareClient
from backend.adapters.deploy_keys import DeployKeyStore
from backend.config import Settings
from backend.config_ingress import IngressSettings
from backend.logging import SecretRedactor
from backend.repositories.database import SessionFactory, UnitOfWork
from backend.services.audit import AuditService
from backend.services.platform_secrets import PlatformSecretService
from backend.services.secret_cipher import SecretCipher
from backend.worker.integrations_handlers import IntegrationDeps, build_integration_handlers
from backend.worker.rpc import Handler

HTTP_TIMEOUT = 20.0


def build_integrations(  # noqa: PLR0913, PLR0917 - one collaborator each
    settings: Settings,
    ingress: IngressSettings,
    session_factory: SessionFactory,
    cipher: SecretCipher,
    redactor: SecretRedactor,
    transport: httpx.BaseTransport | None = None,
) -> Mapping[str, Handler]:
    """The ``integrations.*`` methods; registered whether or not ingress is enabled."""
    http = httpx.Client(timeout=HTTP_TIMEOUT, transport=transport)

    def cloudflare(token: str) -> CloudflareClient:
        return CloudflareClient(token, base_url=ingress.cloudflare_api_base, client=http)

    deps = IntegrationDeps(
        platform_secrets=lambda: PlatformSecretService(
            UnitOfWork(session_factory), cipher, redactor
        ),
        cloudflare=cloudflare,
        audit=lambda: AuditService(UnitOfWork(session_factory)),
        ingress=ingress,
        keys=DeployKeyStore(settings.git_ssh_key_dir),
        webhook_secret_file=settings.github_webhook_secret_file,
    )
    return build_integration_handlers(deps)
