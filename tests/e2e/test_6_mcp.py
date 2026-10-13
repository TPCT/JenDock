"""Step 8: the MCP endpoint with an API token: tools, a database query and scope denials."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tests.e2e.client import Api, Mcp
from tests.e2e.scenario import SLUG, Scenario
from tests.e2e.stack import Stack

pytestmark = pytest.mark.e2e


def _payload(result: dict[str, Any]) -> Any:
    """The JSON a tool returned (structured content, else the text block)."""
    if result.get("structuredContent") is not None:
        return result["structuredContent"]
    return json.loads(result["content"][0]["text"])


def _mcp(stack: Stack, token: str) -> Mcp:
    return Mcp(stack.base_url, token, host=f"127.0.0.1:{stack.api_port}")


def test_15_initialize_list_and_call_tools(stack: Stack, api: Api, scenario: Scenario) -> None:
    client = _mcp(stack, scenario.token)
    info = client.initialize()
    assert info["serverInfo"]["name"]
    names = {t["name"] for t in client.tools()}
    assert {"list_runs", "run_readonly_query"} <= names
    runs = client.tool("list_runs", {"slug": SLUG, "environment": "development"})
    assert not runs.get("isError"), runs
    page = _payload(runs)
    assert page["total"] == 3
    assert page["items"][0]["status"] == "failed"  # the rolled back one is the newest
    query = client.tool(
        "run_readonly_query",
        {
            "slug": SLUG,
            "environment": "development",
            "connection": "main",
            "statement": "SELECT count(*) AS n FROM items",
        },
    )
    assert not query.get("isError"), query
    assert _payload(query)["rows"] == [[3]]
    refused = client.tool(
        "run_readonly_query",
        {
            "slug": SLUG,
            "environment": "development",
            "connection": "main",
            "statement": "DELETE FROM items",
        },
    )
    assert refused["isError"] is True


def test_16_scope_denials(stack: Stack, api: Api, scenario: Scenario) -> None:
    admin_tools = {t["name"] for t in _mcp(stack, scenario.token).tools()}
    created = api.post("/api/tokens", {"name": "e2e-reader", "scopes": ["read"]}, expect=201)
    reader = _mcp(stack, created["token"])
    reader.initialize()
    reader_tools = {t["name"] for t in reader.tools()}
    assert "list_runs" in reader_tools
    hidden = sorted(admin_tools - reader_tools)
    assert hidden, "a read-only token must see fewer tools"
    denied = reader.tool(hidden[0], {})
    assert denied["isError"] is True
    # production data needs the admin scope, a reader gets nothing there
    production = reader.tool(
        "run_readonly_query",
        {
            "slug": SLUG,
            "environment": "production",
            "connection": "main",
            "statement": "SELECT 1",
        },
    )
    assert production["isError"] is True
    # no token, a cookie-only caller and a wrong Host header never reach the tools
    plain = httpx.post(f"{stack.base_url}/mcp", json={}, timeout=30.0)
    assert plain.status_code == 401
    cookie_only = httpx.post(
        f"{stack.base_url}/mcp", json={}, headers={"Cookie": api.cookie}, timeout=30.0
    )
    assert cookie_only.status_code == 401
    wrong_host = httpx.post(
        f"{stack.base_url}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={
            "Authorization": f"Bearer {scenario.token}",
            "Accept": "application/json, text/event-stream",
            "Host": "evil.example",
        },
        timeout=30.0,
    )
    assert wrong_host.status_code in {400, 403, 421}
