"""Login, logout and current session."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from shiftcodes.api.deps import Container, check_origin, client_ip, get_principal
from shiftcodes.api.principal import Principal
from shiftcodes.api.schemas.auth import LoginRequest, SessionResponse, UserResponse
from shiftcodes.domain.errors import AuthenticationError

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
def login(
    body: LoginRequest, request: Request, response: Response, container: Container
) -> SessionResponse:
    """Verify credentials, open a session and set the cookie."""
    check_origin(request, container)
    result = container.auth_service().login(body.username, body.password, client_ip(request))
    container.cookies.set(response, result.session.token)
    user = UserResponse(id=result.user_id, username=result.username)
    return SessionResponse(
        user=user, csrf_token=result.session.csrf_token, expires_at=result.session.expires_at
    )


@router.post("/logout", status_code=204)
def logout(
    principal: Annotated[Principal, Depends(get_principal)], container: Container
) -> Response:
    """Delete the session and clear the cookie."""
    if principal.session is None or principal.session_token is None:
        raise AuthenticationError("no session to end")
    container.auth_service().logout(principal.session_token, principal.session.user_id)
    response = Response(status_code=204)
    container.cookies.delete(response)
    return response


@router.get("/session")
def current_session(principal: Annotated[Principal, Depends(get_principal)]) -> SessionResponse:
    """Return the user and CSRF token of the current session."""
    if principal.session is None:
        raise AuthenticationError("no session")
    return SessionResponse.from_context(principal.session)
