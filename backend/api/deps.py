"""Request-level authentication and authorization dependencies."""

from collections.abc import Callable
from enum import StrEnum
from typing import Annotated

from fastapi import Depends, Request

from backend.api import authz
from backend.api.container import AppContainer
from backend.api.principal import Principal
from backend.domain.access import GLOBAL, Action, Grant, Resource
from backend.domain.enums import EnvironmentName
from backend.domain.errors import (
    AuthenticationError,
    NotFoundError,
    PasswordChangeRequiredError,
    PermissionDeniedError,
    ValidationError,
)
from backend.services.sessions import SessionContext, csrf_matches

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


def _grants(container: AppContainer, user_id: int, privileged: bool) -> tuple[Grant, ...]:
    """Grants matter only to members; owners and admins skip the lookup."""
    return () if privileged else container.access_service().load(user_id)


def _bearer_principal(container: AppContainer, header: str) -> Principal:
    if not header.lower().startswith(BEARER_PREFIX):
        raise AuthenticationError(UNAUTHENTICATED)
    token = container.token_service().authenticate(header[len(BEARER_PREFIX) :].strip())
    if token is None:
        raise AuthenticationError("invalid or expired token")
    grants = _grants(container, token.owner_id, token.owner_role.is_privileged)
    return Principal(token=token, grants=grants)


def _session_principal(
    request: Request, container: AppContainer, cookie: str, *, touch: bool, allow_pending: bool
) -> Principal:
    if request.method not in SAFE_METHODS:
        _check_csrf(request, container, cookie)
    session = container.session_service().resolve(cookie, touch=touch)
    if session is None:
        raise AuthenticationError("session expired or invalid")
    if session.must_change_password and not allow_pending:
        raise PasswordChangeRequiredError("you must change your password before continuing")
    grants = _grants(container, session.user_id, session.role.is_privileged)
    return Principal(session=session, session_token=cookie, grants=grants)


def _authenticate(
    request: Request, container: AppContainer, *, touch: bool, allow_pending: bool = False
) -> Principal:
    """Bearer header wins and disables the cookie entirely; otherwise the cookie is used."""
    header = request.headers.get("authorization")
    if header is not None:
        return _bearer_principal(container, header)
    cookie = request.cookies.get(container.cookies.name)
    if cookie is None:
        raise AuthenticationError(UNAUTHENTICATED)
    return _session_principal(request, container, cookie, touch=touch, allow_pending=allow_pending)


def get_principal(request: Request, container: Container) -> Principal:
    """Authenticate the request; a session's idle timer is extended."""
    return _authenticate(request, container, touch=True)


def get_pending_principal(request: Request, container: Container) -> Principal:
    """Like ``get_principal`` but also accepts a session that must still change its password.

    Only the session, change-password and logout endpoints use it.
    """
    return _authenticate(request, container, touch=True, allow_pending=True)


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


class Target(StrEnum):
    """What a route's path parameters point at."""

    NONE = "none"
    PROJECT = "project"  # {slug}
    ENVIRONMENT = "environment"  # {slug}/{env}
    RUN = "run"  # {run_id}, resolved to its environment
    CONTAINER = "container"  # {slug}/{env}/{container_id}, resolved to its service when needed


def _environment_of(request: Request) -> EnvironmentName:
    try:
        return EnvironmentName(str(request.path_params["env"]))
    except ValueError:
        raise ValidationError("environment must be development or production") from None


def _authorize_request(
    request: Request, container: AppContainer, principal: Principal, action: Action, target: Target
) -> None:
    params = request.path_params
    if target is Target.NONE:
        authz.authorize(principal, action, GLOBAL)
    elif target is Target.PROJECT:
        authz.authorize(principal, action, Resource.of_project(str(params["slug"])))
    elif target is Target.ENVIRONMENT:
        resource = Resource.of_environment(str(params["slug"]), _environment_of(request))
        authz.authorize(principal, action, resource)
    elif target is Target.RUN:
        try:
            run_id = int(params["run_id"])
        except ValueError:
            raise NotFoundError("run not found") from None
        authz.authorize_run(container, principal, action, run_id)
    else:
        authz.authorize_container(
            container,
            principal,
            action,
            str(params["slug"]),
            _environment_of(request),
            str(params["container_id"]),
        )


def require(
    action: Action, target: Target = Target.ENVIRONMENT, *, background: bool = False
) -> Callable[..., Principal]:
    """Dependency factory: authenticate, then ``authorize`` ``action`` on what the route targets.

    This is the only way routers decide permissions (the policy in ``backend.services.policy``
    combines token scopes and the user's role and grants).
    """

    def dependency(
        request: Request,
        container: Container,
        principal: Annotated[Principal, Depends(_source(background=background))],
    ) -> Principal:
        _authorize_request(request, container, principal, action, target)
        return principal

    dependency.access_action = action  # type: ignore[attr-defined]  # read by the route audit test
    return dependency


def session_context(principal: Principal) -> SessionContext:
    """The session of a principal that is known to be a browser session."""
    if principal.session is None:
        raise PermissionDeniedError("this operation requires a browser session")
    return principal.session
