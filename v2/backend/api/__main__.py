"""API server entrypoint: ``python -m backend.api``.

Starts uvicorn programmatically so proxy headers are honoured only for the configured
``trusted_proxies``. Without them the peer address is used, never ``X-Forwarded-For``.
"""

from __future__ import annotations

from dataclasses import dataclass

import uvicorn

from backend.config import Settings, get_settings
from backend.process import apply_shared_umask

APP_IMPORT_PATH = "backend.api.main:app"
HOST = "0.0.0.0"
PORT = 8000


@dataclass(frozen=True, slots=True)
class ServerOptions:
    """The uvicorn options that depend on settings."""

    proxy_headers: bool
    forwarded_allow_ips: str | None


def server_options(settings: Settings) -> ServerOptions:
    """Trust forwarded headers only from ``trusted_proxies``; otherwise from nobody."""
    if not settings.trusted_proxies:
        return ServerOptions(proxy_headers=False, forwarded_allow_ips=None)
    return ServerOptions(True, ",".join(settings.trusted_proxies))


def main() -> None:
    """Run the API with uvicorn."""
    apply_shared_umask()
    options = server_options(get_settings())
    uvicorn.run(
        APP_IMPORT_PATH,
        host=HOST,
        port=PORT,
        proxy_headers=options.proxy_headers,
        forwarded_allow_ips=options.forwarded_allow_ips,
    )


if __name__ == "__main__":
    main()
