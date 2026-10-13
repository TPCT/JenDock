"""Secret schemas. Only ``RevealResponse`` ever carries a value."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from backend.services.secrets import MAX_SECRET_BYTES, SecretInfo


class SetSecretRequest(BaseModel):
    """Body of ``PUT .../secrets/{name}``."""

    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1, max_length=MAX_SECRET_BYTES, repr=False)


class SecretResponse(BaseModel):
    """Secret metadata without value."""

    name: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_info(cls, info: SecretInfo) -> SecretResponse:
        """Build the response from service metadata."""
        return cls(name=info.name, created_at=info.created_at, updated_at=info.updated_at)


class RevealResponse(BaseModel):
    """The plaintext of one secret, returned only by the audited reveal endpoint."""

    name: str
    value: str = Field(repr=False)
