"""What Settings -> Integrations shows: Cloudflare and ingress status, GitHub webhook and keys.

Runs in the API. It never sees a credential: the token and the private deploy keys live in the
worker, which answers with booleans, public keys and test results. Stored settings (ACME email,
public addresses, challenge) are plain database rows and are changed here, audited.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from backend.adapters.integrations_client import (
    CloudflareTest,
    IngressState,
    IntegrationsGateway,
)
from backend.config import Settings
from backend.config_ingress import IngressSettings
from backend.domain.actor import Actor
from backend.domain.clock import Clock, utcnow
from backend.domain.enums import EnvironmentName
from backend.domain.errors import NotFoundError, WorkerUnavailableError
from backend.domain.ingress import CLOUDFLARE_TOKEN_SECRET
from backend.repositories.database import UnitOfWork
from backend.services.lookup import require_environment, require_project
from backend.services.platform_secrets import CLOUDFLARE_TOKEN, PlatformSecretInfo, secret_info
from backend.services.platform_settings import (
    IngressOverrides,
    IngressSettingsPatch,
    PlatformSettingsService,
)

RECENT_DELIVERIES = 10
SUMMARY_WINDOW = timedelta(days=7)
WEBHOOK_PATH = "/api/github/webhook"


@dataclass(frozen=True, slots=True)
class IngressValues:
    """The four editable values: stored, environment default and the one in effect."""

    stored: IngressOverrides
    defaults: IngressOverrides
    effective: IngressOverrides


@dataclass(frozen=True, slots=True)
class Reason:
    """Why ingress cannot work yet (``blocking``) or what is still advisable."""

    code: str
    message: str
    blocking: bool = True


@dataclass(frozen=True, slots=True)
class CloudflareOverview:
    """Everything the Cloudflare section shows."""

    token: PlatformSecretInfo
    values: IngressValues
    ingress: IngressState | None
    reasons: tuple[Reason, ...]

    @property
    def ready(self) -> bool:
        """Whether domains can be applied right now."""
        return not any(reason.blocking for reason in self.reasons)


@dataclass(frozen=True, slots=True)
class DeliveryView:
    """One recorded webhook delivery."""

    delivery_id: str
    event: str
    repository: str | None
    outcome: str
    received_at: datetime


@dataclass(frozen=True, slots=True)
class DeployKeyView:
    """A project's deploy key status: the public key only."""

    slug: str
    name: str
    repository: str
    transport: str
    exists: bool
    public_key: str | None


@dataclass(frozen=True, slots=True)
class GithubOverview:
    """The webhook and deploy keys sections."""

    payload_url: str
    secret_configured: bool | None
    counts: dict[str, int]
    recent: tuple[DeliveryView, ...]
    keys: tuple[DeployKeyView, ...]


def _settings_overrides(base: IngressSettings) -> IngressOverrides:
    return IngressOverrides(
        acme_email=base.acme_email,
        public_ipv4=base.public_ipv4,
        public_ipv6=base.public_ipv6,
        acme_challenge=base.acme_challenge,
    )


def _merge(stored: IngressOverrides, defaults: IngressOverrides) -> IngressOverrides:
    return IngressOverrides(
        acme_email=stored.acme_email or defaults.acme_email,
        public_ipv4=stored.public_ipv4 or defaults.public_ipv4,
        public_ipv6=stored.public_ipv6 or defaults.public_ipv6,
        acme_challenge=stored.acme_challenge or defaults.acme_challenge,
    )


class IntegrationsService:
    """Read models and use cases of the Integrations pages."""

    def __init__(
        self,
        uow: UnitOfWork,
        gateway: IntegrationsGateway,
        settings: Settings,
        ingress: IngressSettings,
        clock: Clock = utcnow,
    ) -> None:
        self._uow = uow
        self._gateway = gateway
        self._settings = settings
        self._ingress = ingress
        self._clock = clock

    # --- Cloudflare / ingress -----------------------------------------------------------------

    def cloudflare(self) -> CloudflareOverview:
        """Token status, values and the reasons ingress cannot work yet."""
        token = secret_info(self._uow, CLOUDFLARE_TOKEN)
        values = self._values()
        state = self._ingress_state()
        return CloudflareOverview(
            token, values, state, self._reasons(token.configured, state, values)
        )

    def readiness(self, slug: str | None, env: EnvironmentName | None) -> CloudflareOverview:
        """Like :meth:`cloudflare`, but an environment's own token secret also counts."""
        overview = self.cloudflare()
        if overview.token.configured or slug is None or env is None:
            return overview
        if not self._environment_has_token(slug, env):
            return overview
        reasons = tuple(r for r in overview.reasons if r.code != "no_token")
        return CloudflareOverview(overview.token, overview.values, overview.ingress, reasons)

    def update_settings(self, patch: IngressSettingsPatch, actor: Actor) -> IngressValues:
        """Store, replace or reset the named values (audited)."""
        PlatformSettingsService(self._uow, self._clock).update(patch, actor)
        return self._values()

    def set_token(self, token: str, actor: Actor) -> PlatformSecretInfo:
        """Hand the token to the worker, which encrypts and stores it."""
        self._gateway.set_cloudflare_token(token, actor)
        return secret_info(self._uow, CLOUDFLARE_TOKEN)

    def clear_token(self, actor: Actor) -> None:
        """Ask the worker to forget the token."""
        self._gateway.clear_cloudflare_token(actor)

    def test(self, actor: Actor) -> CloudflareTest:
        """Verify the stored token with Cloudflare (in the worker) and list its zones."""
        return self._gateway.test_cloudflare(actor)

    def _values(self) -> IngressValues:
        stored = PlatformSettingsService(self._uow, self._clock).overrides()
        defaults = _settings_overrides(self._ingress)
        return IngressValues(stored, defaults, _merge(stored, defaults))

    def _ingress_state(self) -> IngressState | None:
        try:
            return self._gateway.ingress_state(Actor.system("integrations"))
        except WorkerUnavailableError:
            return None

    def _environment_has_token(self, slug: str, env: EnvironmentName) -> bool:
        with self._uow:
            environment = require_environment(self._uow, slug, env)
            return self._uow.secrets.get(environment.id, CLOUDFLARE_TOKEN_SECRET) is not None

    def _reasons(
        self, configured: bool, state: IngressState | None, values: IngressValues
    ) -> tuple[Reason, ...]:
        reasons: list[Reason] = []
        if state is None:
            reasons.append(Reason("worker_unavailable", "The worker is not reachable."))
        else:
            if not state.enabled:
                reasons.append(
                    Reason(
                        "ingress_disabled",
                        "Ingress is switched off on the worker (JENDOCK_INGRESS_ENABLED is not "
                        "true). Start the stack with the compose.ingress.yml overlay.",
                    )
                )
            if not state.helper_reachable:
                detail = state.helper_reason or "the host helper is not reachable"
                reasons.append(Reason("helper_unreachable", f"Host helper: {detail}."))
        if not configured:
            reasons.append(Reason("no_token", "No Cloudflare API token is stored yet."))
        if not values.effective.acme_email:
            reasons.append(
                Reason(
                    "no_acme_email",
                    "Set an ACME contact email before requesting TLS certificates.",
                    blocking=False,
                )
            )
        return tuple(reasons)

    # --- GitHub ------------------------------------------------------------------------------

    def github(self, public_origin: str, actor: Actor) -> GithubOverview:
        """Webhook URL, delivery summary and per-project deploy key status."""
        now = self._clock()
        with self._uow:
            counts = self._uow.deliveries.outcome_counts(now - SUMMARY_WINDOW)
            recent = tuple(
                DeliveryView(d.delivery_id, d.event, d.repository, d.outcome, d.received_at)
                for d in self._uow.deliveries.recent(RECENT_DELIVERIES)
            )
            projects = [
                (p.slug, p.name, p.repository, p.git_transport.value)
                for p in self._uow.projects.list()
            ]
        repositories = sorted({repository for _, _, repository, _ in projects})
        try:
            state = self._gateway.github_state(repositories, actor)
            secret_configured: bool | None = state.webhook_secret_configured
            keys = dict(state.keys)
        except WorkerUnavailableError:
            secret_configured, keys = None, {}
        views = tuple(
            DeployKeyView(
                slug, name, repository, transport, bool(keys.get(repository)), keys.get(repository)
            )
            for slug, name, repository, transport in projects
        )
        return GithubOverview(
            payload_url=public_origin.rstrip("/") + WEBHOOK_PATH,
            secret_configured=secret_configured,
            counts=counts,
            recent=recent,
            keys=views,
        )

    def create_deploy_key(self, slug: str, actor: Actor) -> str:
        """Create the project's deploy key in the worker; the public key comes back."""
        with self._uow:
            project = require_project(self._uow, slug)
            if project.archived_at is not None:
                raise NotFoundError(f"project not found: {slug}")
            repository = project.repository
        return self._gateway.create_deploy_key(repository, actor)
