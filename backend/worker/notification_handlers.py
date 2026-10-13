"""RPC methods of notification channels. They run in the worker, which holds the key.

Params carry webhook URLs and SMTP credentials, so a malformed request is answered with a
generic message that never echoes the input, and results hold ids and outcomes only.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from backend.domain.notifications import ChannelKind
from backend.services.notification_channels import ChannelService
from backend.worker.rpc import BadRequestError, Handler, RpcCall

ChannelServiceFactory = Callable[[], ChannelService]
ConfigValue = str | int | bool | list[str] | None


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _CreateParams(_Params):
    name: str = Field(max_length=200)
    kind: ChannelKind
    config: dict[str, ConfigValue]


class _UpdateParams(_Params):
    id: int
    name: str | None = Field(default=None, max_length=200)
    enabled: bool | None = None
    config: dict[str, ConfigValue] | None = None


class _IdParams(_Params):
    id: int


def _parse[P: _Params](model: type[P], call: RpcCall) -> P:
    try:
        return model.model_validate(call.params)
    except PydanticValidationError as error:
        # Never echo the input: it contains the webhook URL or the SMTP password.
        raise BadRequestError("invalid params") from error


class NotificationHandlers:
    """Adapts ``ChannelService`` to RPC calls; a fresh service per call."""

    def __init__(self, factory: ChannelServiceFactory) -> None:
        self._factory = factory

    def create(self, call: RpcCall) -> object:
        """``notify.channel.create``: ``{"id": ...}``."""
        p = _parse(_CreateParams, call)
        channel = self._factory().create(p.name, p.kind, dict(p.config), call.require_actor())
        return {"id": channel.id}

    def update(self, call: RpcCall) -> object:
        """``notify.channel.update``: partial change; omitted secret fields are kept."""
        p = _parse(_UpdateParams, call)
        channel = self._factory().update(
            p.id,
            call.require_actor(),
            name=p.name,
            enabled=p.enabled,
            config=dict(p.config) if p.config is not None else None,
        )
        return {"id": channel.id}

    def delete(self, call: RpcCall) -> object:
        """``notify.channel.delete``."""
        p = _parse(_IdParams, call)
        self._factory().delete(p.id, call.require_actor())
        return {}

    def test(self, call: RpcCall) -> object:
        """``notify.channel.test``: ``{"ok": bool, "error": str | null}``."""
        p = _parse(_IdParams, call)
        ok, error = self._factory().test(p.id, call.require_actor())
        return {"ok": ok, "error": error}


def build_notification_handlers(factory: ChannelServiceFactory) -> Mapping[str, Handler]:
    """The ``notify.*`` part of the RPC allowlist."""
    handlers = NotificationHandlers(factory)
    return {
        "notify.channel.create": handlers.create,
        "notify.channel.update": handlers.update,
        "notify.channel.delete": handlers.delete,
        "notify.channel.test": handlers.test,
    }
