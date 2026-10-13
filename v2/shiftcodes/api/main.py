"""ASGI entrypoint: ``uvicorn shiftcodes.api.main:app``."""

from __future__ import annotations

from shiftcodes.api.app import create_asgi_app
from shiftcodes.config import get_settings
from shiftcodes.logging import configure_logging

configure_logging(get_settings())
app = create_asgi_app()
