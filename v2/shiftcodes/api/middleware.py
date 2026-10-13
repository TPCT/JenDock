"""ASGI middleware adding security headers to every response."""

from __future__ import annotations

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CONTENT_SECURITY_POLICY = "default-src 'self'; frame-ancestors 'none'; base-uri 'none'"
HSTS = "max-age=31536000; includeSubDomains"


class SecurityHeadersMiddleware:
    """Sets CSP, nosniff, referrer, framing and cache headers (HSTS in production)."""

    def __init__(self, app: ASGIApp, *, hsts: bool) -> None:
        self._app = app
        self._headers = {
            "Content-Security-Policy": CONTENT_SECURITY_POLICY,
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "same-origin",
            "X-Frame-Options": "DENY",
            "Cache-Control": "no-store",
        }
        if hsts:
            self._headers["Strict-Transport-Security"] = HSTS

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in self._headers.items():
                    headers[name] = value
            await send(message)

        await self._app(scope, receive, send_with_headers)
