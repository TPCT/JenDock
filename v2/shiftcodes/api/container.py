"""The application container: every collaborator of the API, wired in one place."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Engine

from shiftcodes.adapters.worker_client import GithubGateway, SecretsGateway, WorkerClient
from shiftcodes.api.cookies import CookiePolicy
from shiftcodes.config import Settings
from shiftcodes.domain.clock import Clock, utcnow
from shiftcodes.execution.logs import LogStore
from shiftcodes.repositories.database import (
    SessionFactory,
    UnitOfWork,
    create_db_engine,
    create_session_factory,
)
from shiftcodes.services.auth import AuthService
from shiftcodes.services.environments import EnvironmentService
from shiftcodes.services.passwords import PasswordHasher
from shiftcodes.services.project_queries import ProjectQueryService
from shiftcodes.services.projects import ProjectService
from shiftcodes.services.rate_limit import LoginRateLimiter
from shiftcodes.services.run_lifecycle import RunLifecycleService
from shiftcodes.services.run_queries import RunQueryService
from shiftcodes.services.run_triggers import RunTriggerService
from shiftcodes.services.sessions import SessionService
from shiftcodes.services.tokens import TokenService
from shiftcodes.services.webhooks import WebhookService


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

    @classmethod
    def build(  # noqa: PLR0913 - one optional override per collaborator
        cls,
        settings: Settings,
        *,
        secrets: SecretsGateway | None = None,
        github: GithubGateway | None = None,
        clock: Clock = utcnow,
        hasher: PasswordHasher | None = None,
        limiter: LoginRateLimiter | None = None,
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
            github=github or worker,
        )

    def uow(self) -> UnitOfWork:
        """Return a fresh unit of work."""
        return UnitOfWork(self.session_factory)

    def session_service(self) -> SessionService:
        """Return a session service with its own unit of work."""
        return SessionService(self.uow(), self.clock)

    def token_service(self) -> TokenService:
        """Return a token service with its own unit of work."""
        return TokenService(self.uow(), self.clock)

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
