"""RPC method implementations. The worker is the only process that decrypts secrets."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from backend.domain.enums import EnvironmentName
from backend.services.secrets import MAX_SECRET_BYTES, SecretInfo, SecretService
from backend.worker.rpc import BadRequestError, Handler, RpcCall

SecretServiceFactory = Callable[[], SecretService]


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _EnvParams(_Params):
    slug: str = Field(max_length=64)
    env: EnvironmentName


class _NameParams(_EnvParams):
    name: str = Field(max_length=128)


class _SetParams(_NameParams):
    value: str = Field(max_length=MAX_SECRET_BYTES)


def _parse[P: _Params](model: type[P], call: RpcCall) -> P:
    try:
        return model.model_validate(call.params, strict=False)
    except PydanticValidationError as error:
        # Never echo the input: params of secrets.set contain the secret value.
        raise BadRequestError("invalid params") from error


def _info_payload(info: SecretInfo) -> dict[str, object]:
    return {
        "name": info.name,
        "created_at": info.created_at.isoformat(),
        "updated_at": info.updated_at.isoformat(),
    }


class SecretHandlers:
    """Adapts ``SecretService`` to RPC calls; a fresh service (and unit of work) per call."""

    def __init__(self, factory: SecretServiceFactory) -> None:
        self._factory = factory

    def set(self, call: RpcCall) -> object:
        """``secrets.set``: create or rotate a secret."""
        params = _parse(_SetParams, call)
        info = self._factory().set(
            params.slug, params.env, params.name, params.value, call.require_actor()
        )
        return _info_payload(info)

    def list(self, call: RpcCall) -> object:
        """``secrets.list``: names and timestamps only."""
        params = _parse(_EnvParams, call)
        call.require_actor()
        return [_info_payload(i) for i in self._factory().list_names(params.slug, params.env)]

    def delete(self, call: RpcCall) -> object:
        """``secrets.delete``: remove one secret."""
        params = _parse(_NameParams, call)
        self._factory().delete(params.slug, params.env, params.name, call.require_actor())
        return {}

    def reveal(self, call: RpcCall) -> object:
        """``secrets.reveal``: plaintext of one secret, audited."""
        params = _parse(_NameParams, call)
        value = self._factory().reveal(params.slug, params.env, params.name, call.require_actor())
        return {"value": value}


def build_handlers(
    factory: SecretServiceFactory, extra: Mapping[str, Handler] | None = None
) -> Mapping[str, Handler]:
    """Return the allowlist of RPC methods; ``extra`` adds methods of other modules."""
    secrets = SecretHandlers(factory)
    return {
        **(extra or {}),
        "ping": lambda _call: {"status": "ok"},
        "secrets.set": secrets.set,
        "secrets.list": secrets.list,
        "secrets.delete": secrets.delete,
        "secrets.reveal": secrets.reveal,
    }
