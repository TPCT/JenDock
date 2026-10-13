"""Serving the built web UI: static files plus a fallback to ``index.html`` for client routes."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from fastapi import FastAPI
from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from backend.domain.errors import ConfigurationError

INDEX = "index.html"
ASSETS_PREFIX = "assets/"
IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
# A missing file with one of these suffixes is a real 404, not a client-side route.
_FILE_SUFFIXES = frozenset(
    {".js", ".mjs", ".css", ".map", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico"}
    | {".woff", ".woff2", ".ttf", ".txt", ".json", ".webmanifest", ".xml"}
)


def _is_api_path(path: str) -> bool:
    return path in {"api", "healthz"} or path.startswith(("api/", "healthz/"))


def _looks_like_file(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in _FILE_SUFFIXES


class SinglePageFiles(StaticFiles):
    """``StaticFiles`` that answers client-side routes with ``index.html``.

    Paths below ``/api`` (and ``/healthz``) are never answered with HTML: they get the JSON
    error envelope of the API. Files under ``/assets`` carry content hashes and are cached for a
    year; everything else keeps the default ``no-store`` of the security middleware, so a new
    deployment is picked up at once.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        """The file, or ``index.html`` for a route-like path that matches no file."""
        if _is_api_path(path):
            raise HTTPException(status_code=404, detail="not found")
        try:
            response = await super().get_response(path, scope)
        except HTTPException as error:
            if error.status_code != 404 or path.startswith(ASSETS_PREFIX) or _looks_like_file(path):
                raise
            return await super().get_response(INDEX, scope)
        if path.startswith(ASSETS_PREFIX):
            response.headers["Cache-Control"] = IMMUTABLE_CACHE
        return response


def mount_frontend(app: FastAPI, dist: Path) -> None:
    """Serve the built UI in ``dist`` at ``/``; call after every API route is registered."""
    if not (dist / INDEX).is_file():
        raise ConfigurationError(f"frontend directory has no {INDEX}: {dist}")
    app.mount("/", SinglePageFiles(directory=dist), name="frontend")
