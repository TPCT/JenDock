"""API token schemas. Responses never contain the token value except at creation."""

from __future__ import annotations

from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from shiftcodes.domain.enums import TokenScope
from shiftcodes.services.tokens import CreatedToken, TokenInfo


class CreateTokenRequest(BaseModel):
    """Body of ``POST /api/tokens``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=120)
    scopes: list[TokenScope] = Field(min_length=1, max_length=len(TokenScope))
    expires_at: AwareDatetime | None = None


class TokenResponse(BaseModel):
    """Token metadata."""

    id: int
    name: str
    prefix: str
    scopes: list[TokenScope]
    created_at: datetime
    last_used_at: datetime | None
    expires_at: datetime | None
    revoked_at: datetime | None

    @classmethod
    def from_info(cls, info: TokenInfo) -> TokenResponse:
        """Build the response from service metadata."""
        return cls(
            id=info.id,
            name=info.name,
            prefix=info.prefix,
            scopes=list(info.scopes),
            created_at=info.created_at,
            last_used_at=info.last_used_at,
            expires_at=info.expires_at,
            revoked_at=info.revoked_at,
        )


class CreatedTokenResponse(BaseModel):
    """A new token: ``token`` is the only time its value is ever returned."""

    token: str
    info: TokenResponse

    @classmethod
    def from_created(cls, created: CreatedToken) -> CreatedTokenResponse:
        """Build the response from a freshly created token."""
        return cls(token=created.value, info=TokenResponse.from_info(created.info))
