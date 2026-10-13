"""The API's view of the worker's ``integrations.*`` methods (credentials never come back)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from backend.adapters.worker_client import WorkerClient
from backend.domain.actor import Actor
from backend.domain.errors import WorkerError, WorkerUnavailableError

TEST_TIMEOUT = 60.0
STATUS_TIMEOUT = 20.0
KEYGEN_TIMEOUT = 45.0


@dataclass(frozen=True, slots=True)
class CloudflareTest:
    """Outcome of verifying the stored token with Cloudflare."""

    ok: bool
    status: str | None
    zones: tuple[str, ...]
    message: str


@dataclass(frozen=True, slots=True)
class IngressState:
    """Whether the worker has ingress enabled and can reach the host helper."""

    enabled: bool
    helper_socket_path: str
    helper_reachable: bool
    helper_reason: str | None


@dataclass(frozen=True, slots=True)
class GithubState:
    """The webhook secret's presence and the public deploy key per repository."""

    webhook_secret_configured: bool
    keys: Mapping[str, str | None]


class IntegrationsGateway(Protocol):
    """Integration operations that only the worker may perform."""

    def set_cloudflare_token(self, token: str, actor: Actor) -> None:
        """Store the platform Cloudflare token (encrypted, worker only)."""

    def clear_cloudflare_token(self, actor: Actor) -> None:
        """Forget the stored token."""

    def test_cloudflare(self, actor: Actor) -> CloudflareTest:
        """Verify the stored token and list the zones it can see."""

    def ingress_state(self, actor: Actor) -> IngressState:
        """Ingress enabled flag and host helper reachability."""

    def github_state(self, repositories: list[str], actor: Actor) -> GithubState:
        """Webhook secret presence and public deploy keys."""

    def create_deploy_key(self, repository: str, actor: Actor) -> str:
        """Create a deploy key for ``repository``; returns the public key only."""


class IntegrationsClient(WorkerClient):
    """Calls the ``integrations.*`` methods of the worker."""

    def set_cloudflare_token(self, token: str, actor: Actor) -> None:
        """``integrations.cloudflare_set_token``."""
        self._call("integrations.cloudflare_set_token", actor, {"token": token})

    def clear_cloudflare_token(self, actor: Actor) -> None:
        """``integrations.cloudflare_clear_token``."""
        self._call("integrations.cloudflare_clear_token", actor, {})

    def test_cloudflare(self, actor: Actor) -> CloudflareTest:
        """``integrations.cloudflare_test``."""
        data = _mapping(self._call("integrations.cloudflare_test", actor, {}, TEST_TIMEOUT))
        try:
            status = data.get("status")
            zones = data["zones"]
            if not isinstance(zones, list):
                raise TypeError("zones")
            return CloudflareTest(
                bool(data["ok"]),
                str(status) if status is not None else None,
                tuple(str(z) for z in zones),
                str(data.get("message", "")),
            )
        except (KeyError, TypeError) as error:
            raise WorkerError("worker returned an unexpected response") from error

    def ingress_state(self, actor: Actor) -> IngressState:
        """``integrations.ingress_status``."""
        data = _mapping(self._call("integrations.ingress_status", actor, {}, STATUS_TIMEOUT))
        try:
            reason = data.get("helper_reason")
            return IngressState(
                bool(data["enabled"]),
                str(data["helper_socket_path"]),
                bool(data["helper_reachable"]),
                str(reason) if reason else None,
            )
        except (KeyError, TypeError) as error:
            raise WorkerError("worker returned an unexpected response") from error

    def github_state(self, repositories: list[str], actor: Actor) -> GithubState:
        """``integrations.github_status``."""
        params = {"repositories": repositories}
        data = _mapping(self._call("integrations.github_status", actor, params, STATUS_TIMEOUT))
        keys = data.get("keys")
        if not isinstance(keys, dict):
            raise WorkerError("worker returned an unexpected response")
        return GithubState(
            bool(data.get("webhook_secret_configured")),
            {str(k): (str(v) if isinstance(v, str) else None) for k, v in keys.items()},
        )

    def create_deploy_key(self, repository: str, actor: Actor) -> str:
        """``integrations.deploy_key_create``."""
        params = {"repository": repository}
        data = _mapping(self._call("integrations.deploy_key_create", actor, params, KEYGEN_TIMEOUT))
        key = data.get("public_key")
        if not isinstance(key, str):
            raise WorkerError("worker returned an unexpected response")
        return key


class UnavailableIntegrations:
    """The default gateway when no worker is wired: every call reports the worker as down."""

    def _fail(self) -> WorkerUnavailableError:
        return WorkerUnavailableError("the worker is not available")

    def set_cloudflare_token(self, token: str, actor: Actor) -> None:  # noqa: ARG002
        """Always raises."""
        raise self._fail()

    def clear_cloudflare_token(self, actor: Actor) -> None:  # noqa: ARG002
        """Always raises."""
        raise self._fail()

    def test_cloudflare(self, actor: Actor) -> CloudflareTest:  # noqa: ARG002
        """Always raises."""
        raise self._fail()

    def ingress_state(self, actor: Actor) -> IngressState:  # noqa: ARG002
        """Always raises."""
        raise self._fail()

    def github_state(self, repositories: list[str], actor: Actor) -> GithubState:  # noqa: ARG002
        """Always raises."""
        raise self._fail()

    def create_deploy_key(self, repository: str, actor: Actor) -> str:  # noqa: ARG002
        """Always raises."""
        raise self._fail()


def _mapping(result: object) -> dict[str, object]:
    if not isinstance(result, dict):
        raise WorkerError("worker returned an unexpected response")
    return result
