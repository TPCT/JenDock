"""Fakes for the discovery tests: paged Cloudflare, project/port catalog, assembled ports.

Nothing here touches the network: Cloudflare is an ``httpx.MockTransport`` that pages like the
real API (and refuses a zone ``per_page`` above 50, as Cloudflare does).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from backend.adapters.cloudflare import CloudflareClient
from backend.config_ingress import IngressSettings
from backend.domain.discovery import ProjectRef, PublishedPort
from backend.domain.enums import EnvironmentName
from backend.services.discovery import DiscoveryPorts
from tests.ingress_support import FakeHelper

SERVER_IP = "203.0.113.7"
ZONE_PAGE_LIMIT = 50


class PagedCloudflare:
    """Zones and DNS records served page by page; every request is recorded."""

    def __init__(self, token: str = "cf-test-token", page_size: int = 2) -> None:  # noqa: S107
        self.token = token
        self.page_size = page_size
        self.zones: list[dict[str, Any]] = []
        self.records: dict[str, list[dict[str, Any]]] = {}
        self.requests: list[httpx.Request] = []
        self.fail_with: int | None = None
        self._next = 1

    def add_zone(self, name: str) -> str:
        """Add an active zone; returns its id."""
        zone_id = f"zone{len(self.zones) + 1:03d}"
        self.zones.append({"id": zone_id, "name": name, "status": "active"})
        self.records[zone_id] = []
        return zone_id

    def add_record(
        self,
        zone: str,
        record_type: str,
        name: str,
        content: str,
        *,
        proxied: bool = False,
        comment: str | None = None,
    ) -> None:
        """Add a record to the zone called ``zone``."""
        zone_id = next(z["id"] for z in self.zones if z["name"] == zone)
        self.records[zone_id].append(
            {
                "id": f"rec{self._next:04d}",
                "type": record_type,
                "name": name,
                "content": content,
                "proxied": proxied,
                "comment": comment,
                "ttl": 1,
            }
        )
        self._next += 1

    def client(self) -> CloudflareClient:
        """A real ``CloudflareClient`` talking to this fake."""
        http = httpx.Client(transport=httpx.MockTransport(self.handle))
        return CloudflareClient(self.token, base_url="https://api.test/client/v4", client=http)

    def mutations(self) -> list[httpx.Request]:
        """Requests that were not reads (discovery must never make one)."""
        return [r for r in self.requests if r.method != "GET"]

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Serve one request."""
        self.requests.append(request)
        if self.fail_with is not None:
            return httpx.Response(self.fail_with, json=_error("boom", self.fail_with))
        if request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(403, json=_error("Invalid token", 9109))
        path = request.url.path.removeprefix("/client/v4").strip("/").split("/")
        params = request.url.params
        if path == ["zones"]:
            if int(params.get("per_page", "20")) > ZONE_PAGE_LIMIT:
                return httpx.Response(400, json=_error("per_page must be at most 50", 1001))
            items = [
                z
                for z in self.zones
                if params.get("name") in (None, z["name"]) and z["status"] == "active"
            ]
            return self._page(items, params)
        if len(path) == 3 and path[0] == "zones" and path[2] == "dns_records":
            items = [
                r
                for r in self.records.get(path[1], [])
                if params.get("type") in (None, r["type"]) and params.get("name") in (None, r["name"])
            ]
            return self._page(items, params)
        return httpx.Response(404, json=_error("not found", 7003))

    def _page(self, items: list[dict[str, Any]], params: httpx.QueryParams) -> httpx.Response:
        page = int(params.get("page", "1"))
        size = min(self.page_size, int(params.get("per_page", "100")))
        pages = max(1, -(-len(items) // size))
        chosen = items[(page - 1) * size : page * size]
        info = {
            "page": page,
            "per_page": size,
            "count": len(chosen),
            "total_count": len(items),
            "total_pages": pages,
        }
        body = {"success": True, "errors": [], "result": chosen, "result_info": info}
        return httpx.Response(200, json=body)


def _error(message: str, code: int) -> dict[str, Any]:
    return {"success": False, "errors": [{"code": code, "message": message}], "result": None}


class FakePorts:
    """The projects and published ports the worker would read from the database and Docker."""

    def __init__(
        self, projects: list[ProjectRef] | None = None, ports: list[PublishedPort] | None = None
    ) -> None:
        self._projects = projects or []
        self._ports = ports or []

    def projects(self) -> list[ProjectRef]:
        """The scripted projects."""
        return list(self._projects)

    def published_ports(self) -> list[PublishedPort]:
        """The scripted published ports."""
        return list(self._ports)


def project_ref(slug: str, name: str | None = None) -> ProjectRef:
    """A project with both environments."""
    environments = (EnvironmentName.DEVELOPMENT, EnvironmentName.PRODUCTION)
    return ProjectRef(slug, name or slug.replace("-", " ").title(), environments)


def published(
    project: str, service: str, container_port: int, host_port: int, env: str = "production"
) -> PublishedPort:
    """One published port."""
    return PublishedPort(project, EnvironmentName(env), service, container_port, host_port)


class PortsBuilder:
    """Assembles ``DiscoveryPorts`` over the fakes; attributes can be changed between calls."""

    def __init__(self, cloudflare: PagedCloudflare, helper: FakeHelper, catalog: FakePorts) -> None:
        self.cloudflare = cloudflare
        self.helper = helper
        self.catalog = catalog
        self.token: str | None = cloudflare.token
        self.settings = IngressSettings(public_ipv4=SERVER_IP)
        self.public_origin = "https://jendock.example.test"
        self.resolved: dict[str, list[str]] = {}

    def resolve(self, host: str) -> list[str]:
        """The scripted DNS answers."""
        return self.resolved.get(host, [])

    def build(self) -> DiscoveryPorts:
        """The ports; ``token``, ``settings`` and ``resolved`` are read at call time."""
        token: Callable[[], str | None] = lambda: self.token  # noqa: E731
        return DiscoveryPorts(
            token=token,
            catalog=lambda _token: self.cloudflare.client(),
            helper=self.helper,
            ports=self.catalog,
            settings=lambda: self.settings,
            resolve=self.resolve,
            public_origin=self.public_origin,
        )


def json_body(request: httpx.Request) -> Any:
    """The decoded JSON body of a recorded request."""
    return json.loads(request.content)
