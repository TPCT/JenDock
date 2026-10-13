"""Wire format shared by the worker RPC server and the API's worker client.

One JSON object per line over a Unix socket. Request:
``{"id": "...", "method": "secrets.set", "actor": {"type": "user", "id": "1"}, "params": {}}``.
Response: ``{"id": "...", "ok": true, "result": ...}`` or
``{"id": "...", "ok": false, "error": {"code": "not_found", "message": "..."}}``.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from shiftcodes.domain.actor import Actor
from shiftcodes.domain.enums import ActorType
from shiftcodes.domain.errors import (
    ConfigurationError,
    ConflictError,
    DomainError,
    NotFoundError,
    ValidationError,
    WorkerError,
)

MAX_MESSAGE_BYTES = 1024 * 1024
MAX_ID_LENGTH = 64


class ErrorCode(StrEnum):
    """Machine-readable failure categories."""

    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    VALIDATION = "validation"
    CONFIGURATION = "configuration"
    BAD_REQUEST = "bad_request"
    UNKNOWN_METHOD = "unknown_method"
    TOO_LARGE = "too_large"
    INTERNAL = "internal"


_DOMAIN_ERRORS: dict[ErrorCode, type[DomainError]] = {
    ErrorCode.NOT_FOUND: NotFoundError,
    ErrorCode.CONFLICT: ConflictError,
    ErrorCode.VALIDATION: ValidationError,
    ErrorCode.CONFIGURATION: ConfigurationError,
}


class RpcActor(BaseModel):
    """The principal on whose behalf the API calls the worker (for audit)."""

    model_config = ConfigDict(extra="forbid")

    type: ActorType
    id: str = Field(min_length=1, max_length=64)

    @classmethod
    def from_actor(cls, actor: Actor) -> RpcActor:
        """Wrap a domain actor; the id is mandatory on the wire."""
        if actor.id is None:
            raise ValidationError("actor id is required")
        return cls(type=actor.type, id=actor.id)

    def to_actor(self) -> Actor:
        """Return the domain actor."""
        return Actor(self.type, self.id)


class RpcRequest(BaseModel):
    """A request line."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=MAX_ID_LENGTH)
    method: str = Field(min_length=1, max_length=64)
    actor: RpcActor | None = None
    # Params are validated per method by the handler's own model.
    params: dict[str, object] = Field(default_factory=dict)


class RpcErrorBody(BaseModel):
    """Error part of a failed response."""

    code: ErrorCode
    message: str


class RpcResponse(BaseModel):
    """A response line; ``result`` is JSON-like and method specific."""

    id: str
    ok: bool
    result: object = None
    error: RpcErrorBody | None = None


def error_code_for(error: Exception) -> ErrorCode:
    """Map a raised exception to its wire code (unknown ones are ``internal``)."""
    for code, error_type in _DOMAIN_ERRORS.items():
        if isinstance(error, error_type):
            return code
    return ErrorCode.INTERNAL


def domain_error_for(error: RpcErrorBody) -> DomainError:
    """Map a wire error back to a domain error."""
    error_type = _DOMAIN_ERRORS.get(error.code)
    if error_type is not None:
        return error_type(error.message)
    return WorkerError(f"worker error ({error.code.value}): {error.message}")
