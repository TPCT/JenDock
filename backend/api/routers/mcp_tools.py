"""``GET /api/mcp/tools``: which MCP tools a selection of token scopes opens up.

Derived from the MCP tool registry itself, so what the settings page shows can never drift from
what the server offers. Browser sessions only (the page that creates tokens).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from backend.api.deps import Container, require_session
from backend.api.principal import Principal
from backend.api.schemas.integrations import McpToolResponse, McpToolsResponse
from backend.domain.enums import TokenScope
from backend.domain.errors import ValidationError
from backend.mcp.registry import ToolRegistry
from backend.mcp.server import MCP_PATH
from backend.mcp.tools import default_registry

router = APIRouter(prefix="/api/mcp", tags=["mcp"])
SessionPrincipal = Annotated[Principal, Depends(require_session())]


@lru_cache
def _registry() -> ToolRegistry:
    return default_registry()


def _parse_scopes(raw: str) -> list[TokenScope]:
    scopes: list[TokenScope] = []
    for part in raw.split(","):
        name = part.strip()
        if not name:
            continue
        try:
            scopes.append(TokenScope(name))
        except ValueError as error:
            raise ValidationError(f"unknown scope: {name}") from error
    return scopes


@router.get("/tools")
def list_tools(
    _principal: SessionPrincipal,
    container: Container,
    scopes: Annotated[str, Query(max_length=200)] = "",
) -> McpToolsResponse:
    """The endpoint URL and the tools available to a token with exactly ``scopes``."""
    tools = _registry().visible_for_scopes(_parse_scopes(scopes))
    return McpToolsResponse(
        endpoint=container.settings.public_origin.rstrip("/") + MCP_PATH,
        enabled=container.settings.mcp_enabled,
        tools=[
            McpToolResponse(
                name=tool.name,
                description=tool.description,
                scope=tool.scope.value,
                read_only=tool.read_only,
            )
            for tool in tools
        ],
    )
