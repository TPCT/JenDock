"""API token management. Browser sessions only: a token can never manage tokens."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from backend.api.deps import Container, Target, require, session_context
from backend.api.paging import Paging
from backend.api.principal import Principal
from backend.api.schemas.tokens import (
    CreatedTokenResponse,
    CreateTokenRequest,
    TokenPage,
    TokenResponse,
)
from backend.domain.access import Action

router = APIRouter(prefix="/api/tokens", tags=["tokens"])
SessionPrincipal = Annotated[Principal, Depends(require(Action.TOKEN_MANAGE, Target.NONE))]


@router.get("")
def list_tokens(
    principal: SessionPrincipal,
    container: Container,
    paging: Paging,
    state: Annotated[str | None, Query(max_length=16)] = None,
) -> TokenPage:
    """A page of the caller's own tokens (metadata only). ``q`` matches name and prefix; ``state``
    is ``active``, ``revoked`` or ``expired``; ``sort`` is ``created`` (default), ``name`` or
    ``last_used``."""
    page = container.token_service().search(paging.query, state, principal.user_id)
    return TokenPage.from_page(page)


@router.post("", status_code=201)
def create_token(
    body: CreateTokenRequest, principal: SessionPrincipal, container: Container
) -> CreatedTokenResponse:
    """Create a token; its value is in this response only."""
    session = session_context(principal)
    created = container.token_service().create(
        body.name, body.scopes, session.user_id, body.expires_at, principal.actor
    )
    return CreatedTokenResponse.from_created(created)


@router.delete("/{token_id}")
def revoke_token(token_id: int, principal: SessionPrincipal, container: Container) -> TokenResponse:
    """Revoke one of the caller's own tokens."""
    revoked = container.token_service().revoke(token_id, principal.actor, principal.user_id)
    return TokenResponse.from_info(revoked)
