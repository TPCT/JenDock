"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.routing import Route
from starlette.types import ASGIApp

from backend.api.container import AppContainer
from backend.api.errors import register_error_handlers
from backend.api.frontend import mount_frontend
from backend.api.middleware import SecurityHeadersMiddleware
from backend.api.routers import (
    access,
    audit,
    auth,
    containers,
    dashboard,
    databases,
    deployments,
    discovery,
    domains,
    environment_power,
    github,
    integrations,
    mcp_tools,
    me,
    notifications,
    pipelines,
    projects,
    resources,
    runs,
    secrets,
    telemetry,
    terminal,
    tokens,
    users,
)
from backend.config import Settings, get_settings
from backend.mcp.server import MCP_PATH, McpEndpoint


def create_app(settings: Settings | None = None, container: AppContainer | None = None) -> FastAPI:
    """Build the API application.

    ``container`` lets tests inject collaborators; by default it is built from settings.
    Building it never reads the secret key file: only the worker holds the key.
    """
    resolved = container.settings if container else settings or get_settings()
    wired = container or AppContainer.build(resolved)
    production = resolved.environment == "production"

    mcp = McpEndpoint(wired) if resolved.mcp_enabled else None

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        if mcp is None:
            yield
        else:
            async with mcp.lifespan():
                yield
        wired.engine.dispose()

    # No Swagger UI: its CDN assets conflict with the strict CSP. The schema is development-only.
    app = FastAPI(
        title="Jendock",
        docs_url=None,
        redoc_url=None,
        openapi_url=None if production else "/openapi.json",
        lifespan=lifespan,
    )
    app.state.container = wired
    register_error_handlers(app)
    app.include_router(auth.router)
    app.include_router(me.router)
    app.include_router(audit.router)
    app.include_router(dashboard.router)
    app.include_router(tokens.router)
    app.include_router(users.router)
    app.include_router(access.router)
    app.include_router(secrets.router)
    app.include_router(databases.router)
    app.include_router(discovery.router)  # before domains: "zones" is not a domain id
    app.include_router(domains.router)
    app.include_router(runs.router)
    app.include_router(containers.router)
    app.include_router(resources.router)
    app.include_router(environment_power.router)
    app.include_router(terminal.router)
    app.include_router(telemetry.router)
    app.include_router(deployments.router)
    app.include_router(projects.router)
    app.include_router(github.router)
    app.include_router(notifications.router)
    app.include_router(integrations.router)
    app.include_router(mcp_tools.router)
    app.include_router(pipelines.environment_router)
    app.include_router(pipelines.editor_router)

    if mcp is not None:
        app.router.routes.append(Route(MCP_PATH, endpoint=mcp))  # bearer tokens only, see mcp/

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    if resolved.frontend_dist is not None:
        mount_frontend(app, resolved.frontend_dist)  # last: it answers every unmatched path
    return app


def wrap_security_headers(app: ASGIApp, settings: Settings) -> ASGIApp:
    """Wrap the whole app so even unhandled 500s (raised outside user middleware) carry headers."""
    return SecurityHeadersMiddleware(app, hsts=settings.environment == "production")


def create_asgi_app(
    settings: Settings | None = None, container: AppContainer | None = None
) -> ASGIApp:
    """Build the ASGI application served by uvicorn: the API inside the security headers."""
    app = create_app(settings, container)
    return wrap_security_headers(app, app.state.container.settings)
