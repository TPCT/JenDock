"""Request and response bodies of the container endpoints.

Responses reuse the wire views of ``backend.containers.wire`` so there is one definition.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from backend.domain.containers import MAX_ENV_VALUE_BYTES, LifecycleAction

EnvValue = Annotated[str, StringConstraints(max_length=MAX_ENV_VALUE_BYTES)]


class ActionRequest(BaseModel):
    """Body of ``POST .../actions``: what to do (step one, nothing happens yet)."""

    model_config = ConfigDict(extra="forbid")

    action: LifecycleAction


class ConfirmRequest(BaseModel):
    """Body of ``POST .../actions/confirm``: the confirmation from step one."""

    model_config = ConfigDict(extra="forbid")

    confirmation: str = Field(min_length=1, max_length=200)


class EnvUpdateRequest(BaseModel):
    """Body of ``PUT .../env``: overrides to set and overrides to drop."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    values: dict[str, EnvValue] = Field(
        default_factory=dict, alias="set", max_length=100, repr=False, description="NAME to value"
    )
    remove: list[str] = Field(default_factory=list, max_length=100)


class LimitsRequest(BaseModel):
    """Body of ``PUT .../limits``: at least one of the two."""

    model_config = ConfigDict(extra="forbid")

    cpus: float | None = Field(default=None, gt=0, le=1024)
    memory: str | None = Field(default=None, min_length=1, max_length=32)


class ResourceLimitsRequest(BaseModel):
    """Body of ``PUT .../resources/{service}``: the complete set of limits of the service.

    A limit left out (or ``null``) is not set. The values are checked against the host's
    capacity by the worker (``422`` with the reason). Memory is in bytes.
    """

    model_config = ConfigDict(extra="forbid")

    cpus: float | None = Field(default=None, gt=0, le=4096)
    memory_bytes: int | None = Field(default=None, gt=0, le=2**50)
    memory_reservation_bytes: int | None = Field(default=None, gt=0, le=2**50)
    cpu_shares: int | None = Field(default=None, ge=1, le=10_000_000)
    apply_now: bool = False


class TerminalTicketRequest(BaseModel):
    """Body of ``POST .../terminal/tickets``; ``terminal_id`` reattaches a detached terminal."""

    model_config = ConfigDict(extra="forbid")

    terminal_id: str | None = Field(default=None, min_length=1, max_length=64)


class TerminalTicketResponse(BaseModel):
    """The ticket to present as the first WebSocket message."""

    ticket: str
    expires_at: datetime
    websocket_path: str
    container_id: str
