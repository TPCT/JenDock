"""The application container: every collaborator of the API, wired in one place."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import Engine

from backend.adapters.container_client import ContainerClient, ContainersGateway
from backend.adapters.ingress_client import IngressClient, IngressGateway, UnavailableIngress
from backend.adapters.inspector_client import InspectorGateway, WorkerInspectorClient
from backend.adapters.integrations_client import (
    IntegrationsClient,
    IntegrationsGateway,
    UnavailableIntegrations,
)
from backend.adapters.notification_client import NotificationGateway, WorkerNotificationClient
from backend.adapters.worker_client import (
    GithubGateway,
    RepositoryGateway,
    SecretsGateway,
    UnavailableRepositoryGateway,
    WorkerClient,
)
from backend.api.cookies import CookiePolicy
from backend.api.schemas.runs import LogChunkResponse
from backend.api.terminal_tickets import TicketStore
from backend.config import Settings
from backend.config_ingress import IngressSettings, get_ingress_settings
from backend.domain.clock import Clock, utcnow
from backend.execution.logs import CHUNK_BYTES, LogStore
from backend.repositories.database import (
    SessionFactory,
    UnitOfWork,
    create_db_engine,
    create_session_factory,
)
from backend.services.access import AccessService
from backend.services.auth import AuthService
from backend.services.certificates import RenewalPolicy
from backend.services.database_connections import DatabaseConnectionService
from backend.services.deployments import DeploymentService
from backend.services.domain_status import DomainStatusService
from backend.services.domains import DomainService
from backend.services.environments import EnvironmentService
from backend.services.integrations import IntegrationsService
from backend.services.notification_rules import NotificationQueryService, NotificationRuleService
from backend.services.passwords import PasswordHasher
from backend.services.pipeline_editor import PipelineDocumentService, PipelineEditorService
from backend.services.pipelines import PipelineService
from backend.services.project_queries import ProjectQueryService
from backend.services.projects import ProjectService
from backend.services.rate_limit import LoginRateLimiter, webhook_failure_limiter
from backend.services.run_lifecycle import RunLifecycleService
from backend.services.run_queries import RunQueryService
from backend.services.run_triggers import RunTriggerService
from backend.services.sessions import SessionService
from backend.services.tokens import TokenService
from backend.services.users import UserService
from backend.services.webhooks import WebhookService


@dataclass(frozen=True, slots=True)
class AppContainer:
    """Shared, thread-safe collaborators. Services are built per request.

    Services hold a non-reentrant ``UnitOfWork``, so each request gets fresh ones.
    The API never holds the secret key: secrets go through ``secrets`` (the worker).
    """

    settings: Settings
    engine: Engine
    session_factory: SessionFactory
    clock: Clock
    hasher: PasswordHasher
    limiter: LoginRateLimiter
    secrets: SecretsGateway
    cookies: CookiePolicy
    # Webhook verification and branch lookups: both run in the worker (keys and git live there).
    github: GithubGateway
    ingress: IngressGateway = field(default_factory=UnavailableIngress)
    ingress_settings: IngressSettings = field(default_factory=IngressSettings)
    # Cloudflare credential, test connection and deploy keys: all worker-side.
    integrations: IntegrationsGateway = field(default_factory=UnavailableIntegrations)
    repository: RepositoryGateway = field(default_factory=UnavailableRepositoryGateway)
    # Failed webhook signatures per client IP (separate from the login limiter).
    webhook_limiter: LoginRateLimiter = field(default_factory=webhook_failure_limiter)
    # Database inspector calls to the worker; ``None`` means the default socket client.
    inspector: InspectorGateway | None = None
    # Container management through the worker; ``None`` means "the default client" (see
    # ``container_gateway``). Tickets of the terminal WebSocket live here, in process memory.
    containers: ContainersGateway | None = None
    terminal_tickets: TicketStore = field(default_factory=TicketStore)
    # Notification channel configuration goes through the worker (it encrypts); ``None`` means
    # the default socket client (see ``notification_gateway``).
    notifications: NotificationGateway | None = None

    @classmethod
    def build(  # noqa: PLR0913 - one optional override per collaborator
        cls,
        settings: Settings,
        *,
        secrets: SecretsGateway | None = None,
        inspector: InspectorGateway | None = None,
        github: GithubGateway | None = None,
        clock: Clock = utcnow,
        hasher: PasswordHasher | None = None,
        limiter: LoginRateLimiter | None = None,
        containers: ContainersGateway | None = None,
    ) -> AppContainer:
        """Create the default wiring; tests override individual collaborators."""
        engine = create_db_engine(settings.database_url)
        worker = WorkerClient(settings.worker_socket_path)
        return cls(
            settings=settings,
            engine=engine,
            session_factory=create_session_factory(engine),
            clock=clock,
            hasher=hasher or PasswordHasher(),
            limiter=limiter or LoginRateLimiter(clock),
            secrets=secrets or worker,
            cookies=CookiePolicy.for_settings(settings),
            inspector=inspector,
            containers=containers or ContainerClient(worker),
            terminal_tickets=TicketStore(clock),
            ingress=IngressClient(settings.worker_socket_path),
            integrations=IntegrationsClient(settings.worker_socket_path),
            ingress_settings=get_ingress_settings(),
            github=github or worker,
            repository=worker,
            webhook_limiter=webhook_failure_limiter(clock),
        )

    def container_gateway(self) -> ContainersGateway:
        """Container management (always through the worker; the API has no Docker access)."""
        if self.containers is not None:
            return self.containers
        return ContainerClient(WorkerClient(self.settings.worker_socket_path))

    def notification_gateway(self) -> NotificationGateway:
        """Channel writes and test sends (always through the worker, which holds the key)."""
        if self.notifications is not None:
            return self.notifications
        return WorkerNotificationClient(WorkerClient(self.settings.worker_socket_path))

    def notification_queries(self) -> NotificationQueryService:
        """Channel and delivery history read models."""
        return NotificationQueryService(self.uow())

    def notification_rules(self) -> NotificationRuleService:
        """Rule sets of the default, project and environment scopes."""
        return NotificationRuleService(self.uow(), self.clock)

    def uow(self) -> UnitOfWork:
        """Return a fresh unit of work."""
        return UnitOfWork(self.session_factory)

    def session_service(self) -> SessionService:
        """Return a session service with its own unit of work."""
        return SessionService(self.uow(), self.clock)

    def token_service(self) -> TokenService:
        """Return a token service with its own unit of work."""
        return TokenService(self.uow(), self.clock)

    def access_service(self) -> AccessService:
        """Return the access grant service with its own unit of work."""
        return AccessService(self.uow(), self.clock)

    def user_service(self) -> UserService:
        """Return the user management service with its own unit of work."""
        return UserService(self.uow(), self.hasher, self.clock)

    def auth_service(self) -> AuthService:
        """Return a login service with its own unit of work."""
        return AuthService(
            self.uow(), self.hasher, self.limiter, self.session_service(), self.clock
        )

    def run_queries(self) -> RunQueryService:
        """Return a run read-model service with its own unit of work."""
        return RunQueryService(self.uow())

    def run_lifecycle(self) -> RunLifecycleService:
        """Return a run lifecycle service with its own unit of work."""
        return RunLifecycleService(self.uow(), self.clock)

    def database_connections(self) -> DatabaseConnectionService:
        """Return the connection configuration service (passwords go through the worker)."""
        return DatabaseConnectionService(self.uow(), self.secrets, self.clock)

    def inspector_gateway(self) -> InspectorGateway:
        """Return the worker gateway for database inspection."""
        if self.inspector is not None:
            return self.inspector
        return WorkerInspectorClient(WorkerClient(self.settings.worker_socket_path))

    def deployment_service(self) -> DeploymentService:
        """Return a deployment history service with its own unit of work."""
        return DeploymentService(self.uow(), self.clock)

    def domain_service(self) -> DomainService:
        """Return a domain service with its own unit of work."""
        return DomainService(
            self.uow(), self.clock, allow_wildcards=self.ingress_settings.allow_wildcards
        )

    def domain_status_service(self) -> DomainStatusService:
        """Return the read-only domain status service."""
        policy = RenewalPolicy(
            renew_before=timedelta(days=self.ingress_settings.renew_before_days),
            retry_after=timedelta(hours=self.ingress_settings.renew_retry_hours),
        )
        return DomainStatusService(self.uow(), self.domain_service(), policy, self.clock)

    def integrations_service(self) -> IntegrationsService:
        """Return the Integrations pages' service (settings, status, webhook summary)."""
        return IntegrationsService(
            self.uow(), self.integrations, self.settings, self.ingress_settings, self.clock
        )

    def pipeline_editor(self) -> PipelineEditorService:
        """Return the parse/render/validate service with its own unit of work."""
        return PipelineEditorService(self.uow())

    def pipeline_documents(self) -> PipelineDocumentService:
        """Return the stored-pipeline service (repository file, versions, dry run)."""
        return PipelineDocumentService(
            self.uow(),
            PipelineService(self.uow(), clock=self.clock),
            self.pipeline_editor(),
            self.repository,
        )

    def log_store(self) -> LogStore:
        """Step logs written by the worker (shared data volume, read-only use here)."""
        return LogStore(self.settings.data_dir / "logs")

    def project_queries(self) -> ProjectQueryService:
        """Return the project and run list read model with its own unit of work."""
        return ProjectQueryService(self.uow())

    def run_triggers(self) -> RunTriggerService:
        """Return the manual run and retry service with its own unit of work."""
        return RunTriggerService(self.uow(), self.github, self.clock)

    def webhook_service(self) -> WebhookService:
        """Return the webhook delivery service with its own unit of work."""
        return WebhookService(self.uow(), self.clock)

    def project_service(self) -> ProjectService:
        """Return the project use cases with their own unit of work."""
        return ProjectService(self.uow(), self.clock)

    def environment_service(self) -> EnvironmentService:
        """Return the environment use cases with their own unit of work."""
        return EnvironmentService(self.uow(), self.clock)

    def step_log(self, run_id: int, step_id: int, offset: int) -> LogChunkResponse:
        """A bounded chunk of a step's log starting at byte ``offset`` (already masked)."""
        step = self.run_queries().require_step(run_id, step_id)
        chunk = self.log_store().read_chunk(run_id, step_id, offset, CHUNK_BYTES)
        return LogChunkResponse.from_chunk(chunk, step_finished=step.status.is_finished)
