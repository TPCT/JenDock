"""RPC methods behind Settings -> Integrations: Cloudflare credential, test, GitHub deploy keys.

Only the worker holds the Cloudflare token and the private deploy keys. Every method returns
metadata, public keys and test results; no response, audit record or log line carries a
credential. Params that contain the token are never echoed back on a validation failure.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from backend.adapters.cloudflare import CloudflareClient, CloudflareError
from backend.adapters.deploy_keys import DeployKeyStore
from backend.adapters.host_helper_client import HostHelperClient, HostHelperError
from backend.config_ingress import IngressSettings
from backend.domain.actor import Actor
from backend.domain.audit import AuditTarget
from backend.domain.errors import DomainError, NotFoundError
from backend.services.audit import AuditService
from backend.services.platform_secrets import CLOUDFLARE_TOKEN, PlatformSecretService
from backend.worker.rpc import BadRequestError, Handler, RpcCall

MAX_REPOSITORIES = 200
MAX_TOKEN = 512


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _TokenParams(_Params):
    token: str = Field(min_length=1, max_length=MAX_TOKEN, repr=False)


class _RepositoryParams(_Params):
    repository: str = Field(min_length=3, max_length=160)


class _StatusParams(_Params):
    repositories: list[str] = Field(default_factory=list, max_length=MAX_REPOSITORIES)


def _parse[P: _Params](model: type[P], call: RpcCall) -> P:
    try:
        return model.model_validate(call.params)
    except PydanticValidationError as error:
        # Never echo the input: ``integrations.cloudflare_set_token`` carries the token.
        raise BadRequestError("invalid params") from error


@dataclass(frozen=True, slots=True)
class IntegrationDeps:
    """Collaborators of the handlers; tests substitute each one."""

    platform_secrets: Callable[[], PlatformSecretService]
    cloudflare: Callable[[str], CloudflareClient]
    audit: Callable[[], AuditService]
    ingress: IngressSettings
    keys: DeployKeyStore
    webhook_secret_file: Path | None


class IntegrationHandlers:
    """Adapts the integration use cases to RPC calls."""

    def __init__(self, deps: IntegrationDeps) -> None:
        self._deps = deps

    def set_token(self, call: RpcCall) -> object:
        """``integrations.cloudflare_set_token``: store (encrypted) the platform token."""
        params = _parse(_TokenParams, call)
        info = self._deps.platform_secrets().set(
            CLOUDFLARE_TOKEN, params.token, call.require_actor()
        )
        return {"configured": info.configured, "updated_at": _iso(info.updated_at)}

    def clear_token(self, call: RpcCall) -> object:
        """``integrations.cloudflare_clear_token``: forget the platform token."""
        self._deps.platform_secrets().clear(CLOUDFLARE_TOKEN, call.require_actor())
        return {"configured": False}

    def test_token(self, call: RpcCall) -> object:
        """``integrations.cloudflare_test``: verify the token and list the zones it can see."""
        actor = call.require_actor()
        token = self._deps.platform_secrets().resolve(CLOUDFLARE_TOKEN)
        if token is None:
            return {"ok": False, "status": None, "zones": [], "message": "no token is stored"}
        client = self._deps.cloudflare(token)
        zones: list[str] = []
        try:
            status = client.verify_token()
            zones = client.list_zone_names() if status == "active" else []
        except CloudflareError as error:
            result: dict[str, object] = {
                "ok": False,
                "status": None,
                "zones": [],
                "message": str(error),
            }
        else:
            result = {
                "ok": status == "active",
                "status": status,
                "zones": zones,
                "message": "" if status == "active" else f"the token is {status}",
            }
        self._record(
            actor,
            "integration.cloudflare_test",
            AuditTarget("platform", "cloudflare"),
            {
                "ok": result["ok"],
                "zone_count": len(zones) if result["ok"] else 0,
            },
        )
        return result

    def ingress_status(self, call: RpcCall) -> object:
        """``integrations.ingress_status``: is ingress enabled and is the host helper reachable."""
        call.require_actor()
        settings = self._deps.ingress
        reachable, reason = _helper_state(settings)
        return {
            "enabled": settings.enabled,
            "helper_socket_path": str(settings.helper_socket_path),
            "helper_reachable": reachable,
            "helper_reason": reason,
        }

    def github_status(self, call: RpcCall) -> object:
        """``integrations.github_status``: webhook secret present, public deploy keys per repo."""
        params = _parse(_StatusParams, call)
        call.require_actor()
        existing = set(self._deps.keys.repositories())
        keys: dict[str, str | None] = {}
        for repository in params.repositories:
            keys[repository] = self._public_key(repository) if repository in existing else None
        return {"webhook_secret_configured": self._webhook_secret_present(), "keys": keys}

    def create_deploy_key(self, call: RpcCall) -> object:
        """``integrations.deploy_key_create``: new key; 409 when one exists; public key only."""
        params = _parse(_RepositoryParams, call)
        actor = call.require_actor()
        created = self._deps.keys.create(params.repository)
        self._record(
            actor,
            "deploy_key.create",
            AuditTarget("repository", params.repository),
            {"repository": params.repository},
        )
        return {"public_key": created.public_key}

    def _public_key(self, repository: str) -> str | None:
        try:
            return self._deps.keys.public_key(repository)
        except (NotFoundError, DomainError):
            return None

    def _webhook_secret_present(self) -> bool:
        path = self._deps.webhook_secret_file
        try:
            return path is not None and bool(path.read_bytes().strip())
        except OSError:
            return False

    def _record(
        self, actor: Actor, action: str, target: AuditTarget, details: dict[str, object]
    ) -> None:
        self._deps.audit().record(actor, action, target, details)


def _iso(value: object) -> str | None:
    isoformat = getattr(value, "isoformat", None)
    return str(isoformat()) if callable(isoformat) else None


def _helper_state(settings: IngressSettings) -> tuple[bool, str | None]:
    path = settings.helper_socket_path
    try:
        present = path.exists()
    except OSError:
        present = False
    if not present:
        return False, f"the socket {path} does not exist in the worker"
    try:
        HostHelperClient(path, timeout=3.0).ping()
    except HostHelperError as error:
        return False, f"the host helper did not answer ({error.code})"
    return True, None


def build_integration_handlers(deps: IntegrationDeps) -> Mapping[str, Handler]:
    """The method table merged into the worker's RPC server (always registered)."""
    handlers = IntegrationHandlers(deps)
    return {
        "integrations.cloudflare_set_token": handlers.set_token,
        "integrations.cloudflare_clear_token": handlers.clear_token,
        "integrations.cloudflare_test": handlers.test_token,
        "integrations.ingress_status": handlers.ingress_status,
        "integrations.github_status": handlers.github_status,
        "integrations.deploy_key_create": handlers.create_deploy_key,
    }
