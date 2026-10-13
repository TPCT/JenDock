"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from starlette.types import ASGIApp

from shiftcodes.api.container import AppContainer
from shiftcodes.api.errors import register_error_handlers
from shiftcodes.api.middleware import SecurityHeadersMiddleware
from shiftcodes.api.routers import auth, databases, runs, secrets, tokens
from shiftcodes.config import Settings, get_settings


def create_app(settings: Settings | None = None, container: AppContainer | None = None) -> FastAPI:
    """Build the API application.

    ``container`` lets tests inject collaborators; by default it is built from settings.
    Building it never reads the secret key file: only the worker holds the key.
    """
    resolved = container.settings if container else settings or get_settings()
    wired = container or AppContainer.build(resolved)
    production = resolved.environment == "production"

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        wired.engine.dispose()

    # No Swagger UI: its CDN assets conflict with the strict CSP. The schema is development-only.
    app = FastAPI(
        title="Shiftcodes",
        docs_url=None,
        redoc_url=None,
        openapi_url=None if production else "/openapi.json",
        lifespan=lifespan,
    )
    app.state.container = wired
    register_error_handlers(app)
    app.include_router(auth.router)
    app.include_router(tokens.router)
    app.include_router(secrets.router)
    app.include_router(databases.router)
    app.include_router(runs.router)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

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
