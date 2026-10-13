"""Every HTTP endpoint names a policy action (or is one of the few that authenticate otherwise)."""

from __future__ import annotations

from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute

from backend.api.app import create_app
from backend.api.container import AppContainer

# Login and the webhook authenticate with credentials/signatures, the rest of /api/auth are the
# session endpoints that must stay reachable while a password change is pending.
OPEN = {
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/logout"),
    ("GET", "/api/auth/session"),
    ("POST", "/api/auth/change-password"),
    ("POST", "/api/github/webhook"),
}


def _has_action(dependant: Dependant) -> bool:
    if getattr(dependant.call, "access_action", None) is not None:
        return True
    return any(_has_action(sub) for sub in dependant.dependencies)


def test_every_api_route_is_authorized_by_the_policy(container: AppContainer) -> None:
    app = create_app(container=container)
    unguarded = []
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api"):
            continue
        for method in route.methods or ():
            if (method, route.path) not in OPEN and not _has_action(route.dependant):
                unguarded.append((method, route.path))
    assert unguarded == []
