"""Login, logout and current session."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from backend.api.deps import (
    Container,
    check_origin,
    client_ip,
    get_pending_principal,
    session_context,
)
from backend.api.principal import Principal
from backend.api.schemas.auth import ChangePasswordRequest, LoginRequest, SessionResponse
from backend.domain.errors import AuthenticationError
from backend.services.users import WrongPasswordError

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.post("/login")
def login(
    body: LoginRequest, request: Request, response: Response, container: Container
) -> SessionResponse:
    """Verify credentials, open a session and set the cookie."""
    check_origin(request, container)
    result = container.auth_service().login(body.username, body.password, client_ip(request))
    container.cookies.set(response, result.session.token)
    resolved = container.session_service().resolve(result.session.token, touch=False)
    if resolved is None:
        raise AuthenticationError("session expired or invalid")
    grants = (
        () if resolved.role.is_privileged else container.access_service().load(resolved.user_id)
    )
    principal = Principal(session=resolved, session_token=result.session.token, grants=grants)
    return SessionResponse.from_principal(principal)


@router.post("/logout", status_code=204)
def logout(
    principal: Annotated[Principal, Depends(get_pending_principal)], container: Container
) -> Response:
    """Delete the session and clear the cookie."""
    if principal.session is None or principal.session_token is None:
        raise AuthenticationError("no session to end")
    container.auth_service().logout(principal.session_token, principal.session.user_id)
    response = Response(status_code=204)
    container.cookies.delete(response)
    return response


@router.get("/session")
def current_session(
    principal: Annotated[Principal, Depends(get_pending_principal)],
) -> SessionResponse:
    """Return the user, CSRF token, role and effective permissions of the current session."""
    if principal.session is None:
        raise AuthenticationError("no session")
    return SessionResponse.from_principal(principal)


@router.post("/change-password")
def change_password(
    body: ChangePasswordRequest,
    request: Request,
    principal: Annotated[Principal, Depends(get_pending_principal)],
    container: Container,
) -> SessionResponse:
    """Change the caller's own password (also the forced change after an admin reset).

    Needs the current password (wrong ones are rate limited like login failures); the user's
    other sessions end and the change is audited.
    """
    context = session_context(principal)
    limiter, key, ip = container.limiter, f"password-change:{context.user_id}", client_ip(request)
    limiter.check(key, ip)  # 429 after repeated wrong current passwords (same limits as login)
    try:
        container.user_service().change_password(
            context.user_id,
            body.current_password,
            body.new_password,
            principal.actor,
            keep_session_id=context.session_id,
        )
    except WrongPasswordError:
        limiter.record_failure(key, ip)
        raise
    limiter.reset_username(key)
    fresh = container.session_service().resolve(principal.session_token or "", touch=True)
    if fresh is None:
        raise AuthenticationError("session expired or invalid")
    return SessionResponse.from_principal(
        Principal(session=fresh, session_token=principal.session_token, grants=principal.grants)
    )
