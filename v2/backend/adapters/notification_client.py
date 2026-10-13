"""The API's view of notification channel configuration: ``notify.channel.*`` calls to the worker.

Channels hold secrets (webhook URLs, SMTP credentials), so the API never writes them itself:
it forwards the request to the worker, which validates, encrypts and stores. Only ids and the
outcome of a test come back.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from backend.adapters.worker_client import WorkerClient
from backend.domain.actor import Actor
from backend.domain.errors import WorkerError
from backend.domain.notifications import ChannelKind

# A test message may wait for an SMTP or HTTP server (10 s each) plus connection setup.
TEST_TIMEOUT = 30.0


class NotificationGateway(Protocol):
    """Channel operations the API needs; implemented by ``WorkerNotificationClient``."""

    def create_channel(
        self, name: str, kind: ChannelKind, config: Mapping[str, object], actor: Actor
    ) -> int:
        """Store a new channel; returns its id."""

    def update_channel(
        self,
        channel_id: int,
        actor: Actor,
        *,
        name: str | None,
        enabled: bool | None,
        config: Mapping[str, object] | None,
    ) -> int:
        """Change a channel; returns its id."""

    def delete_channel(self, channel_id: int, actor: Actor) -> None:
        """Remove a channel."""

    def test_channel(self, channel_id: int, actor: Actor) -> tuple[bool, str | None]:
        """Send a test message; returns ``(ok, error)``."""


class WorkerNotificationClient:
    """Implements ``NotificationGateway`` over the worker RPC socket."""

    def __init__(self, client: WorkerClient) -> None:
        self._client = client

    def create_channel(
        self, name: str, kind: ChannelKind, config: Mapping[str, object], actor: Actor
    ) -> int:
        """``notify.channel.create``."""
        params = {"name": name, "kind": kind.value, "config": dict(config)}
        return _id(self._client.call("notify.channel.create", actor, params))

    def update_channel(
        self,
        channel_id: int,
        actor: Actor,
        *,
        name: str | None,
        enabled: bool | None,
        config: Mapping[str, object] | None,
    ) -> int:
        """``notify.channel.update`` (only the given fields are sent)."""
        params: dict[str, object] = {"id": channel_id}
        if name is not None:
            params["name"] = name
        if enabled is not None:
            params["enabled"] = enabled
        if config is not None:
            params["config"] = dict(config)
        return _id(self._client.call("notify.channel.update", actor, params))

    def delete_channel(self, channel_id: int, actor: Actor) -> None:
        """``notify.channel.delete``."""
        self._client.call("notify.channel.delete", actor, {"id": channel_id})

    def test_channel(self, channel_id: int, actor: Actor) -> tuple[bool, str | None]:
        """``notify.channel.test``."""
        result = self._client.call(
            "notify.channel.test", actor, {"id": channel_id}, timeout=TEST_TIMEOUT
        )
        if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
            raise WorkerError("worker returned an unexpected response")
        error = result.get("error")
        return result["ok"], error if isinstance(error, str) else None


def _id(result: object) -> int:
    value = result.get("id") if isinstance(result, dict) else None
    if not isinstance(value, int) or isinstance(value, bool):
        raise WorkerError("worker returned an unexpected response")
    return value
