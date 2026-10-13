"""Builds the worker's ingress services (Cloudflare, ACME, host helper) from settings.

Services own a non-reentrant unit of work, so the factory creates fresh ones per call. The
Cloudflare token is read from the environment's secret store (``CLOUDFLARE_API_TOKEN``) only
while a call needs it, in the worker, which is the only process that holds the secret key.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import httpx

from backend.adapters.acme.client import AcmeClient, AcmeConfig, ChallengeSolver
from backend.adapters.acme.issuer import AcmeIssuer
from backend.adapters.acme.jws import AccountKey, load_or_create_account_key
from backend.adapters.acme.solvers import Dns01CloudflareSolver, Http01HelperSolver
from backend.adapters.cloudflare import CloudflareClient, CloudflareDns
from backend.adapters.host_helper_client import HostHelper, HostHelperClient
from backend.adapters.probes import SocketDnsResolver, StdlibHttpProber
from backend.config import Settings
from backend.config_ingress import IngressSettings
from backend.domain.errors import ConfigurationError
from backend.domain.ingress import CLOUDFLARE_TOKEN_SECRET
from backend.hosthelper.names import WILDCARD_PREFIX
from backend.logging import SecretRedactor
from backend.repositories.database import SessionFactory, UnitOfWork
from backend.repositories.models import Domain
from backend.services.certificates import CertificateService, RenewalPolicy
from backend.services.domain_checks import CheckProbes, DomainCheckService
from backend.services.domains import DomainService
from backend.services.ingress import IngressPorts, IngressService
from backend.services.renewal import RenewalScheduler
from backend.services.secret_cipher import SecretCipher
from backend.services.secrets import SecretService

PRIVATE_DIR_NAME = "worker-private"
ACCOUNT_KEY_NAME = "acme-account.key"
HTTP_TIMEOUT = 30.0


class IngressFactory:
    """Creates the ingress services with real (or, in tests, substituted) collaborators."""

    def __init__(  # noqa: PLR0913, PLR0917 - the wiring needs each collaborator once
        self,
        settings: Settings,
        ingress_settings: IngressSettings,
        session_factory: SessionFactory,
        cipher: SecretCipher,
        redactor: SecretRedactor,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._data_dir = settings.data_dir
        self._ingress = ingress_settings
        self._session_factory = session_factory
        self._cipher = cipher
        self._redactor = redactor
        self._http = httpx.Client(timeout=HTTP_TIMEOUT, transport=transport)
        self._acme_http = httpx.Client(
            timeout=HTTP_TIMEOUT, verify=_verify(ingress_settings), transport=transport
        )
        self.helper: HostHelper = HostHelperClient(ingress_settings.helper_socket_path)

    def close(self) -> None:
        """Release the HTTP connection pools."""
        self._http.close()
        self._acme_http.close()

    def domains(self) -> DomainService:
        """A domain service with its own unit of work."""
        return DomainService(
            UnitOfWork(self._session_factory), allow_wildcards=self._ingress.allow_wildcards
        )

    def certificates(self) -> CertificateService:
        """A certificate service issuing through ACME with the configured challenge."""
        policy = RenewalPolicy(
            renew_before=timedelta(days=self._ingress.renew_before_days),
            retry_after=timedelta(hours=self._ingress.renew_retry_hours),
        )
        return CertificateService(
            UnitOfWork(self._session_factory), self.helper, self._issuer_for, policy
        )

    def ingress_service(self) -> IngressService:
        """The preview/apply/remove orchestrator."""
        ports = IngressPorts(self.helper, self._dns_for)
        return IngressService(self.domains(), self.certificates(), ports, self._ingress)

    def check_service(self) -> DomainCheckService:
        """The status checker."""
        probes = CheckProbes(SocketDnsResolver(), StdlibHttpProber(), self.helper)
        return DomainCheckService(UnitOfWork(self._session_factory), probes, self._ingress)

    def renewal_scheduler(self) -> RenewalScheduler:
        """The loop renewing certificates before they expire."""
        return RenewalScheduler(self.certificates(), self._ingress.renewal_interval_seconds)

    # --- collaborators --------------------------------------------------------------------

    def _cloudflare(self, domain: Domain) -> CloudflareClient:
        secrets = SecretService(UnitOfWork(self._session_factory), self._cipher, self._redactor)
        values = secrets.resolve_for_run(domain.environment_id, [CLOUDFLARE_TOKEN_SECRET])
        return CloudflareClient(
            values[CLOUDFLARE_TOKEN_SECRET],
            base_url=self._ingress.cloudflare_api_base,
            client=self._http,
        )

    def _dns_for(self, domain: Domain) -> CloudflareDns:
        return CloudflareDns(self._cloudflare(domain))

    def _issuer_for(self, domain: Domain) -> AcmeIssuer:
        if not self._ingress.acme_email:
            raise ConfigurationError("JENDOCK_INGRESS_ACME_EMAIL is not set")
        key = AccountKey(load_or_create_account_key(self._account_key_path()))
        config = AcmeConfig(
            directory_url=self._ingress.acme_directory_url,
            email=self._ingress.acme_email,
            poll_timeout=self._ingress.acme_poll_timeout_seconds,
        )
        client = AcmeClient(config, key, self._acme_http)
        return AcmeIssuer(client, self._solver_for(domain))

    def _solver_for(self, domain: Domain) -> ChallengeSolver:
        if self._ingress.acme_challenge == "http-01":
            if domain.hostname.startswith(WILDCARD_PREFIX):
                raise ConfigurationError("wildcard certificates need the dns-01 challenge")
            return Http01HelperSolver(self.helper)
        return Dns01CloudflareSolver(
            self._cloudflare(domain), propagation_wait=self._ingress.dns_propagation_wait_seconds
        )

    def _account_key_path(self) -> Path:
        return self._ingress.acme_account_key_file or (
            self._data_dir / PRIVATE_DIR_NAME / ACCOUNT_KEY_NAME
        )


def _verify(settings: IngressSettings) -> str | bool:
    return str(settings.acme_ca_bundle) if settings.acme_ca_bundle else True
