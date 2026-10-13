"""The MCP endpoint: the official SDK's streamable HTTP server behind a bearer-token boundary.

Mounted in the API process at ``/mcp`` (one process, one database, no second deployment unit;
the tools call the same services as the REST routers and never hold anything the API does not).
Stateless JSON responses, so every request stands alone and carries its own token.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from contextvars import ContextVar
from urllib.parse import urlsplit

import anyio
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from backend.api.container import AppContainer
from backend.api.principal import Principal
from backend.mcp.dispatcher import ToolDispatcher
from backend.mcp.registry import ToolRegistry
from backend.mcp.tools import default_registry

logger = logging.getLogger(__name__)
MCP_PATH = "/mcp"
SERVER_NAME = "Jendock"
MAX_BODY_BYTES = 1024 * 1024
MAX_TOKEN_HEADER = 512
BEARER = "bearer "
_principal: ContextVar[Principal | None] = ContextVar("mcp_principal", default=None)


def _require_principal() -> Principal:
    principal = _principal.get()
    if principal is None:  # the boundary always sets it; never answer without an identity
        raise RuntimeError("MCP request without an authenticated principal")
    return principal


def build_server(dispatcher: ToolDispatcher) -> Server[None]:
    """The SDK server whose two handlers delegate to the dispatcher (in a worker thread,
    because services use blocking database access)."""

    async def list_tools(_ctx: object, _params: object) -> types.ListToolsResult:
        principal = _require_principal()
        tools = await anyio.to_thread.run_sync(dispatcher.list_tools, principal)
        return types.ListToolsResult(tools=tools)

    async def call_tool(_ctx: object, params: types.CallToolRequestParams) -> types.CallToolResult:
        principal = _require_principal()
        arguments: Mapping[str, object] | None = params.arguments
        return await anyio.to_thread.run_sync(dispatcher.call, principal, params.name, arguments)

    return Server(
        SERVER_NAME,
        version="1.0",
        instructions=(
            "Tools of the Jendock CI/CD platform. Start with list_projects. To run a "
            "pipeline manually call preview_commit, show the commit to the user, then "
            "trigger_run with that exact commit_sha."
        ),
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


class McpEndpoint:
    """ASGI app for ``/mcp``: checks Origin and the bearer token, then hands over to the SDK.

    No cookies are ever read: a request is authenticated by ``Authorization: Bearer jdt_...``
    alone, and the token's scopes decide which tools exist for it.
    """

    def __init__(self, container: AppContainer, registry: ToolRegistry | None = None) -> None:
        self._container = container
        self._registry = registry or default_registry()
        settings = container.settings
        origin = settings.public_origin.rstrip("/")
        hosts = [urlsplit(origin).netloc, *settings.mcp_allowed_hosts]
        self._origin = origin
        server = build_server(ToolDispatcher(self._registry, container))
        self._sdk = server.streamable_http_app(
            streamable_http_path=MCP_PATH,
            json_response=True,
            stateless_http=True,
            max_request_body_size=MAX_BODY_BYTES,
            transport_security=TransportSecuritySettings(
                enable_dns_rebinding_protection=True,
                allowed_hosts=hosts,
                allowed_origins=[origin],
            ),
        )

    @property
    def registry(self) -> ToolRegistry:
        """The tools this endpoint serves."""
        return self._registry

    def lifespan(self) -> AbstractAsyncContextManager[object]:
        """Start the SDK's session manager; run inside the API's lifespan."""
        return self._lifespan()

    @asynccontextmanager
    async def _lifespan(self) -> AsyncIterator[None]:
        async with self._sdk.router.lifespan_context(self._sdk):
            yield

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            raise RuntimeError("the MCP endpoint only serves HTTP")
        failure = self._origin_failure(scope)
        if failure is not None:
            await failure(scope, receive, send)
            return
        principal = await self._authenticate(scope)
        if principal is None:
            response = JSONResponse(
                {
                    "error": {
                        "code": "unauthenticated",
                        "message": "a valid bearer token is required",
                    }
                },
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="jendock-mcp"'},
            )
            await response(scope, receive, send)
            return
        marker = _principal.set(principal)
        try:
            forwarded: Scope = {**scope, "path": MCP_PATH, "raw_path": MCP_PATH.encode()}
            forwarded["root_path"] = ""
            await self._sdk(forwarded, receive, send)
        finally:
            _principal.reset(marker)

    def _origin_failure(self, scope: Scope) -> ASGIApp | None:
        """Browsers send ``Origin``; the only one allowed is the public origin (spec: servers
        must validate it against DNS rebinding). Clients that send none (agents) pass."""
        values = [v.decode("latin1") for k, v in scope["headers"] if k == b"origin"]
        if len(values) > 1 or (values and values[0] != self._origin):
            return JSONResponse(
                {"error": {"code": "forbidden", "message": "origin not allowed"}}, status_code=403
            )
        return None

    async def _authenticate(self, scope: Scope) -> Principal | None:
        headers = [v.decode("latin1") for k, v in scope["headers"] if k == b"authorization"]
        if len(headers) != 1 or len(headers[0]) > MAX_TOKEN_HEADER:
            return None
        header = headers[0]
        if not header.lower().startswith(BEARER):
            return None
        value = header[len(BEARER) :].strip()
        token = await anyio.to_thread.run_sync(self._container.token_service().authenticate, value)
        return Principal(token=token) if token is not None else None
