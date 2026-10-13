"""API token management. Browser sessions only: a token can never manage tokens."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from shiftcodes.api.deps import Container, require_session
from shiftcodes.api.principal import Principal
from shiftcodes.api.schemas.tokens import CreatedTokenResponse, CreateTokenRequest, TokenResponse

router = APIRouter(prefix="/api/tokens", tags=["tokens"])
SessionPrincipal = Annotated[Principal, Depends(require_session())]


@router.get("")
def list_tokens(_principal: SessionPrincipal, container: Container) -> list[TokenResponse]:
    """List tokens (metadata only)."""
    return [TokenResponse.from_info(info) for info in container.token_service().list()]


@router.post("", status_code=201)
def create_token(
    body: CreateTokenRequest, principal: SessionPrincipal, container: Container
) -> CreatedTokenResponse:
    """Create a token; its value is in this response only."""
    session = principal.session
    if session is None:
        raise RuntimeError("session principal without session")
    created = container.token_service().create(
        body.name, body.scopes, session.user_id, body.expires_at, principal.actor
    )
    return CreatedTokenResponse.from_created(created)


@router.delete("/{token_id}")
def revoke_token(token_id: int, principal: SessionPrincipal, container: Container) -> TokenResponse:
    """Revoke a token."""
    return TokenResponse.from_info(container.token_service().revoke(token_id, principal.actor))
