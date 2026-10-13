"""ASGI entrypoint: ``uvicorn backend.api.main:app``."""

from __future__ import annotations

from backend.api.app import create_asgi_app
from backend.config import get_settings
from backend.logging import configure_logging

configure_logging(get_settings())
app = create_asgi_app()
