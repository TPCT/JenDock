"""MCP tools of domain discovery: list_domains (global), discover_domains, adopt_domain."""

from __future__ import annotations

from backend.api.principal import Principal
from backend.domain.access import AccessRole, GlobalRole
from backend.domain.actor import Actor
from backend.domain.enums import TokenScope
from tests.access_support import add_user, grant
from tests.test_discovery_api import Stack, stack  # noqa: F401 - the fixture
from tests.test_mcp_domains import _call, _principal

ADOPT = {
    "slug": "demo",
    "environment": "production",
    "hostname": "demo.example.com",
    "service": "web",
    "container_port": 8080,
    "published_port": 20001,
}


def test_discover_then_list_unlinked_suggestions(stack: Stack) -> None:  # noqa: F811
    container = stack.container
    admin = _principal(container, TokenScope.ADMIN)
    state = _call(container, admin, "discover_domains", {})
    assert state["hosts"] == 3 and state["cloudflare"]["state"] == "ok"
    reader = _principal(container, TokenScope.READ)
    listed = _call(container, reader, "list_domains", {"link": "suggested"})
    assert [i["hostname"] for i in listed["items"]] == ["api.example.com", "demo.example.com"]
    assert listed["items"][0]["suggestion"]["confidence"] == "high"
    assert stack.cloudflare.mutations() == []


def test_adopt_domain_and_bulk_adopt(stack: Stack) -> None:  # noqa: F811
    container = stack.container
    admin = _principal(container, TokenScope.ADMIN)
    _call(container, admin, "discover_domains", {})
    adopted = _call(container, admin, "adopt_domain", ADOPT)
    assert adopted["origin"] == "adopted" and adopted["status"] == "active"
    rest = _call(container, admin, "adopt_suggested_domains", {})
    assert [d["hostname"] for d in rest["adopted"]] == ["api.example.com"]
    own = _call(container, admin, "list_domains", {"slug": "demo", "environment": "production"})
    assert [d["hostname"] for d in own["items"]] == ["demo.example.com"]


def test_read_tokens_cannot_discover_or_adopt_and_members_cannot_list_everything(
    stack: Stack,  # noqa: F811
) -> None:
    container = stack.container
    reader = _principal(container, TokenScope.READ)
    for name, args in (("discover_domains", {}), ("adopt_domain", ADOPT)):
        assert _call(container, reader, name, args)["error"]["code"] == "forbidden", name
    assert stack.cloudflare.requests == []
    member = add_user(container, "maint", GlobalRole.MEMBER)
    grant(container, "demo", member, role=AccessRole.MAINTAINER)
    token = container.token_service().create(
        "agent", [TokenScope.READ, TokenScope.ADMIN], member, None, Actor.system("t")
    )
    authenticated = container.token_service().authenticate(token.value)
    assert authenticated is not None
    principal = Principal(token=authenticated, grants=container.access_service().load(member))
    assert _call(container, principal, "list_domains", {})["error"]["code"] == "forbidden"
    assert (
        _call(container, principal, "adopt_suggested_domains", {})["error"]["code"] == "forbidden"
    )
    stack.admin.post("/api/domains/discovery/refresh")
    assert _call(container, principal, "adopt_domain", ADOPT)["origin"] == "adopted"
