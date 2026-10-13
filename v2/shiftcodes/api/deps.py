"""Request-level authentication and authorization dependencies."""

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Request

from shiftcodes.api.container import AppContainer
from shiftcodes.api.principal import Principal
from shiftcodes.domain.enums import TokenScope
from shiftcodes.domain.errors import AuthenticationError, PermissionDeniedError
from shiftcodes.services.sessions import csrf_matches

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
CSRF_HEADER = "X-CSRF-Token"
BEARER_PREFIX = "bearer "
UNAUTHENTICATED = "authentication required"


def get_container(request: Request) -> AppContainer:
    """Return the container stored on the application."""
    container: AppContainer = request.app.state.container
    return container


Container = Annotated[AppContainer, Depends(get_container)]


def client_ip(request: Request) -> str:
    """Return the peer address (behind a proxy, uvicorn applies X-Forwarded-For)."""
    return request.client.host if request.client else "unknown"


def check_origin(request: Request, container: AppContainer) -> None:
    """Require ``Origin`` to equal the configured public origin exactly."""
    expected = container.settings.public_origin.rstrip("/")
    if request.headers.get("origin") != expected:
        raise PermissionDeniedError("origin not allowed")


def _check_csrf(request: Request, container: AppContainer, session_token: str) -> None:
    check_origin(request, container)
    supplied = request.headers.get(CSRF_HEADER, "")
    if not csrf_matches(session_token, supplied):
        raise PermissionDeniedError("invalid CSRF token")


def _bearer_principal(container: AppContainer, header: str) -> Principal:
    if not header.lower().startswith(BEARER_PREFIX):
        raise AuthenticationError(UNAUTHENTICATED)
    token = container.token_service().authenticate(header[len(BEARER_PREFIX) :].strip())
    if token is None:
        raise AuthenticationError("invalid or expired token")
    return Principal(token=token)


def _session_principal(
    request: Request, container: AppContainer, cookie: str, *, touch: bool
) -> Principal:
    if request.method not in SAFE_METHODS:
        _check_csrf(request, container, cookie)
    session = container.session_service().resolve(cookie, touch=touch)
    if session is None:
        raise AuthenticationError("session expired or invalid")
    return Principal(session=session, session_token=cookie)


def _authenticate(request: Request, container: AppContainer, *, touch: bool) -> Principal:
    """Bearer header wins and disables the cookie entirely; otherwise the cookie is used."""
    header = request.headers.get("authorization")
    if header is not None:
        return _bearer_principal(container, header)
    cookie = request.cookies.get(container.cookies.name)
    if cookie is None:
        raise AuthenticationError(UNAUTHENTICATED)
    return _session_principal(request, container, cookie, touch=touch)


def get_principal(request: Request, container: Container) -> Principal:
    """Authenticate the request; a session's idle timer is extended."""
    return _authenticate(request, container, touch=True)


def get_background_principal(request: Request, container: Container) -> Principal:
    """Authenticate a background request (event stream) without extending the idle timer."""
    return _authenticate(request, container, touch=False)


def _source(*, background: bool) -> Callable[..., Principal]:
    return get_background_principal if background else get_principal


def require_session(*, background: bool = False) -> Callable[..., Principal]:
    """Dependency factory: only a browser session may call (never a token)."""

    def dependency(
        principal: Annotated[Principal, Depends(_source(background=background))],
    ) -> Principal:
        if not principal.is_session:
            raise PermissionDeniedError("this operation requires a browser session")
        return principal

    return dependency


def require_scope(scope: TokenScope, *, background: bool = False) -> Callable[..., Principal]:
    """Dependency factory: the caller must hold ``scope`` (sessions always do)."""

    def dependency(
        principal: Annotated[Principal, Depends(_source(background=background))],
    ) -> Principal:
        if not principal.has_scope(scope):
            raise PermissionDeniedError(f"missing scope: {scope.value}")
        return principal

    return dependency
