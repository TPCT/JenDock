"""Discovery and adoption end to end: API -> worker RPC -> services with fake Cloudflare/Apache."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from sqlalchemy import Engine

from backend.adapters.discovery_client import DiscoveryClient
from backend.adapters.host_helper_client import HostHelperError, VhostEntry
from backend.api.container import AppContainer
from backend.domain.access import AccessRole, GlobalRole
from backend.domain.enums import EnvironmentName
from backend.repositories.database import UnitOfWork, create_session_factory
from backend.services.adopted_checks import AdoptedCheckService
from backend.services.discovery import DiscoveryService
from backend.services.projects import ProjectService
from backend.worker.discovery_handlers import build_discovery_handlers
from backend.worker.rpc import RpcServer
from tests.access_support import Session, add_user, grant, sign_in
from tests.api_support import (
    FakeClock,
    create_user,
    make_client,
    make_container,
    make_settings,
)
from tests.discovery_support import (
    SERVER_IP,
    FakePorts,
    PagedCloudflare,
    PortsBuilder,
    project_ref,
    published,
)
from tests.ingress_support import FakeHelper
from tests.test_domain_checks import GOOD, FakeProber

DEV = EnvironmentName.DEVELOPMENT
BASE = "/api/projects/demo/environments/production/domains"


@dataclass
class Stack:
    container: AppContainer
    ports: PortsBuilder
    cloudflare: PagedCloudflare
    helper: FakeHelper
    prober: FakeProber
    admin: Session

    def refresh(self) -> dict[str, object]:
        response = self.admin.post("/api/domains/discovery/refresh")
        assert response.status_code == 200, response.text
        return dict(response.json())


def _site(file: str, names: list[str], port: int | None, ssl: bool = True) -> VhostEntry:
    return VhostEntry(file, False, tuple(names), (443,), "127.0.0.1", port, ssl)


@pytest.fixture
def stack(
    tmp_path: Path, socket_dir: Path, engine: Engine, clock: FakeClock, project: str
) -> Iterator[Stack]:
    factory = create_session_factory(engine)
    ProjectService(UnitOfWork(factory)).create("other", "Other", "octo/other")
    cloudflare = PagedCloudflare()
    cloudflare.add_zone("example.com")
    cloudflare.add_record("example.com", "A", "demo.example.com", SERVER_IP, proxied=True)
    cloudflare.add_record("example.com", "A", "api.example.com", SERVER_IP)
    cloudflare.add_record("example.com", "A", "mystery.example.com", SERVER_IP)
    cloudflare.add_record("example.com", "A", "elsewhere.example.com", "198.51.100.1")
    helper = FakeHelper()
    helper.existing_sites = [
        _site("demo.conf", ["demo.example.com"], 20001),
        _site("api.conf", ["api.example.com"], 20002),
        _site("mystery.conf", ["mystery.example.com"], 4000),
    ]
    catalog = FakePorts(
        [project_ref("demo"), project_ref("other")],
        [published("demo", "web", 8080, 20001), published("other", "api", 3000, 20002)],
    )
    ports = PortsBuilder(cloudflare, helper, catalog)
    prober = FakeProber(GOOD)
    built = ports.build()
    handlers = build_discovery_handlers(
        lambda: DiscoveryService(UnitOfWork(factory), built, clock),
        lambda: AdoptedCheckService(UnitOfWork(factory), built, prober, clock),
    )
    socket_path = socket_dir / "d.sock"
    server = RpcServer(socket_path, handlers)
    server.start()
    try:
        settings = make_settings(tmp_path, socket_path=socket_path)
        container = replace(
            make_container(settings, engine, clock), discovery=DiscoveryClient(socket_path)
        )
        create_user(container)
        yield Stack(container, ports, cloudflare, helper, prober, sign_in(container, "admin"))
    finally:
        server.stop()


def _names(response: object) -> list[str]:
    return [item["hostname"] for item in response.json()["items"]]  # type: ignore[attr-defined]


def test_refresh_then_list_with_filters_search_and_paging(stack: Stack) -> None:
    empty = stack.admin.get("/api/domains/discovered").json()
    assert empty["total"] == 0
    state = stack.refresh()
    assert state["cloudflare"] == {"state": "ok", "message": None}  # type: ignore[comparison-overlap]
    assert state["hosts"] == 3 and state["adoptable"] == 2 and state["zones"] == ["example.com"]
    assert state["server_ip"] == SERVER_IP
    page = stack.admin.get("/api/domains/discovered").json()
    assert [i["hostname"] for i in page["items"]] == [
        "api.example.com",
        "demo.example.com",
        "mystery.example.com",
    ]
    demo = page["items"][1]
    assert demo["status"] == "suggested" and demo["suggestion"]["project"] == "demo"
    assert demo["suggestion"]["confidence"] == "high"
    assert demo["dns"]["proxied"] is True and demo["vhost"]["upstream_port"] == 20001
    assert page["items"][2]["status"] == "unlinked"  # nothing matches port 4000
    assert _names(stack.admin.get("/api/domains/discovered?q=DEMO")) == ["demo.example.com"]
    assert len(_names(stack.admin.get("/api/domains/discovered?link=suggested"))) == 2
    assert _names(stack.admin.get("/api/domains/discovered?link=unlinked")) == [
        "mystery.example.com"
    ]
    assert stack.admin.get("/api/domains/discovered?zone=nope.test").json()["total"] == 0
    second = stack.admin.get("/api/domains/discovered?per_page=2&page=2").json()
    assert second["pages"] == 2 and _names(
        stack.admin.get("/api/domains/discovered?per_page=2&page=2")
    ) == ["mystery.example.com"]
    assert stack.cloudflare.mutations() == []


def test_adopt_links_without_touching_dns_or_apache(stack: Stack) -> None:
    stack.refresh()
    body = {
        "hostname": "demo.example.com",
        "service": "web",
        "container_port": 8080,
        "published_port": 20001,
    }
    created = stack.admin.post(f"{BASE}/adopt", body)
    assert created.status_code == 201, created.text
    domain = created.json()
    assert (domain["origin"], domain["status"], domain["dns_proxied"], domain["tls_enabled"]) == (
        "adopted",
        "active",
        True,
        True,
    )
    assert domain["vhost_file"] == "demo.conf" and domain["adopted_at"]
    assert stack.cloudflare.mutations() == []
    assert not any("put" in c or "remove" in c for c in stack.helper.calls)
    linked = stack.admin.get("/api/domains/discovered?link=linked").json()["items"]
    assert [i["link"]["project"] for i in linked] == ["demo"]
    assert linked[0]["link"]["origin"] == "adopted"
    assert stack.admin.post(f"{BASE}/adopt", body).status_code == 409  # already linked
    listed = stack.admin.get(BASE).json()
    assert [d["hostname"] for d in listed] == ["demo.example.com"]


def test_adopting_an_unknown_name_or_a_wrong_service_is_refused(stack: Stack) -> None:
    stack.refresh()
    ghost = {
        "hostname": "ghost.example.com",
        "service": "web",
        "container_port": 1,
        "published_port": 2,
    }
    assert stack.admin.post(f"{BASE}/adopt", ghost).status_code == 404
    with stack.container.uow() as uow:
        env = uow.environments.get_by_project_slug("demo", EnvironmentName.PRODUCTION)
        assert env is not None
        env.services = ["web"]
    wrong = {**ghost, "hostname": "demo.example.com", "service": "db"}
    assert stack.admin.post(f"{BASE}/adopt", wrong).status_code == 422


def test_bulk_adopt_only_takes_certain_matches(stack: Stack) -> None:
    stack.refresh()
    result = stack.admin.post("/api/domains/adopt-suggested", {})
    assert result.status_code == 200, result.text
    adopted = {d["hostname"] for d in result.json()["adopted"]}
    assert adopted == {"api.example.com", "demo.example.com"}  # mystery.* has no certain match
    again = stack.admin.post("/api/domains/adopt-suggested", {"hostnames": ["mystery.example.com"]})
    assert (
        again.json()["adopted"] == []
        and again.json()["skipped"][0]["hostname"] == "mystery.example.com"
    )
    state = stack.admin.get("/api/domains/discovery").json()
    assert (state["linked"], state["adoptable"]) == (2, 0)
    with stack.container.uow() as uow:
        other = uow.domains.get_by_hostname("api.example.com")
        assert other is not None and other.service == "api" and other.container_port == 3000


def test_adopted_domains_are_never_applied_or_removed_by_jendock(stack: Stack) -> None:
    stack.refresh()
    stack.admin.post("/api/domains/adopt-suggested", {})
    domain = stack.admin.get(BASE).json()[0]
    path = f"{BASE}/{domain['id']}"
    assert stack.admin.post(f"{path}/preview", {"operation": "apply"}).status_code == 409
    assert stack.admin.post(f"{path}/apply", {"plan_hash": "a" * 64}).status_code == 409
    assert stack.admin.post(f"{path}/remove", {"plan_hash": "a" * 64}).status_code == 409
    assert stack.admin.post(f"{path}/certificate").status_code == 409
    assert stack.admin.patch(path, {"dns_proxied": False}).status_code == 409
    assert stack.admin.patch(path, {"published_port": 20009}).status_code == 200
    assert stack.cloudflare.mutations() == []
    # deleting only unlinks: the Cloudflare record and the Apache site are untouched
    assert stack.admin.delete(path).status_code == 204
    assert stack.admin.get(BASE).json() == []
    assert stack.admin.get("/api/domains/discovered?link=linked").json()["total"] == 1  # the other


def test_check_of_an_adopted_domain_reports_dns_vhost_https_and_backend(stack: Stack) -> None:
    stack.refresh()
    stack.admin.post("/api/domains/adopt-suggested", {})
    domain = next(d for d in stack.admin.get(BASE).json() if d["hostname"] == "demo.example.com")
    result = stack.admin.post(f"{BASE}/{domain['id']}/check")
    assert result.status_code == 200, result.text
    parts = result.json()["results"]
    assert result.json()["ok"] is True
    assert "proxied by Cloudflare" in parts["dns"]["detail"] and parts["dns"]["proxied"] is True
    assert "demo.conf" in parts["vhost"]["detail"] and parts["vhost"]["ok"] is True
    assert parts["https"]["ok"] is True and parts["backend"]["ok"] is True
    assert stack.prober.calls[-1] == ("demo.example.com", True)
    stored = next(d for d in stack.admin.get(BASE).json() if d["id"] == domain["id"])
    assert stored["check"]["results"]["vhost"]["ok"] is True


def test_check_without_the_helper_or_token_skips_those_parts(stack: Stack) -> None:
    stack.refresh()
    stack.admin.post("/api/domains/adopt-suggested", {})
    stack.helper.discover_error = HostHelperError("unavailable", "gone")
    stack.ports.token = None
    domain = stack.admin.get(BASE).json()[0]
    parts = stack.admin.post(f"{BASE}/{domain['id']}/check").json()["results"]
    assert parts["vhost"]["skipped"] is True
    assert "Install the host helper to see Apache routing" in parts["vhost"]["detail"]
    assert parts["backend"]["skipped"] is True
    assert "connect Cloudflare" in parts["dns"]["detail"]


def test_a_missing_helper_and_missing_token_are_reported_by_refresh(stack: Stack) -> None:
    stack.helper.discover_error = HostHelperError("unavailable", "x")
    stack.ports.token = None
    state = stack.refresh()
    assert state["cloudflare"]["state"] == "no_token"  # type: ignore[index]
    assert state["helper"]["state"] == "missing"  # type: ignore[index]


def test_zone_list_and_lookup_for_the_new_domain_form(stack: Stack) -> None:
    zones = stack.admin.get(f"{BASE}/zones")
    assert zones.status_code == 200 and zones.json() == {
        "configured": True,
        "zones": ["example.com"],
        "message": "",
    }
    taken = stack.admin.get(f"{BASE}/lookup?hostname=demo.example.com").json()
    assert taken["zone"] == "example.com" and taken["records"][0]["content"] == SERVER_IP
    assert taken["records"][0]["managed_by_jendock"] is False and taken["in_use_by"] is None
    free = stack.admin.get(f"{BASE}/lookup?hostname=new.example.com").json()
    assert free["records"] == [] and free["in_use_by"] is None
    stack.refresh()
    stack.admin.post("/api/domains/adopt-suggested", {})
    used = stack.admin.get("/api/domains/lookup?hostname=demo.example.com").json()
    assert used["in_use_by"]["project"] == "demo"
    assert stack.admin.get("/api/domains/zones").json()["zones"] == ["example.com"]
    assert stack.admin.get(f"{BASE}/lookup?hostname=not a host").status_code == 422


# --- authorization ---------------------------------------------------------------------------


def test_only_owners_and_admins_see_and_refresh_discovery(stack: Stack) -> None:
    stack.refresh()
    maintainer = add_user(stack.container, "maint", GlobalRole.MEMBER)
    grant(stack.container, "demo", maintainer, role=AccessRole.MAINTAINER)
    add_user(stack.container, "boss", GlobalRole.ADMIN)
    member = sign_in(stack.container, "maint")
    for path in ("/api/domains/discovered", "/api/domains/discovery", "/api/domains/zones"):
        assert member.get(path).status_code == 403, path
    assert member.post("/api/domains/discovery/refresh").status_code == 403
    assert member.post("/api/domains/adopt-suggested", {}).status_code == 403
    boss = sign_in(stack.container, "boss")
    assert boss.get("/api/domains/discovered").status_code == 200
    assert boss.post("/api/domains/discovery/refresh").status_code == 200


def test_a_maintainer_adopts_into_their_own_project_only(stack: Stack) -> None:
    stack.refresh()
    uid = add_user(stack.container, "maint", GlobalRole.MEMBER)
    grant(stack.container, "demo", uid, role=AccessRole.MAINTAINER)
    viewer = add_user(stack.container, "viewer", GlobalRole.MEMBER)
    grant(stack.container, "demo", viewer, role=AccessRole.VIEWER)
    body = {
        "hostname": "demo.example.com",
        "service": "web",
        "container_port": 8080,
        "published_port": 20001,
    }
    member = sign_in(stack.container, "maint")
    elsewhere = member.post("/api/projects/other/environments/production/domains/adopt", body)
    assert elsewhere.status_code == 404  # a project they cannot even see
    assert sign_in(stack.container, "viewer").post(f"{BASE}/adopt", body).status_code == 403
    assert member.get(f"{BASE}/zones").status_code == 200  # the form works for maintainers
    assert member.post(f"{BASE}/adopt", body).status_code == 201


def test_tokens_need_the_right_scope_and_never_exceed_their_owner(stack: Stack) -> None:
    stack.refresh()
    made = stack.admin.post("/api/tokens", {"name": "r", "scopes": ["read"]})
    reader = {"Authorization": f"Bearer {made.json()['token']}"}
    anonymous = make_client(stack.container)  # no cookie: only the bearer token counts
    assert anonymous.get("/api/domains/discovered", headers=reader).status_code == 200
    refresh = anonymous.post("/api/domains/discovery/refresh", headers=reader)
    assert refresh.status_code == 403  # refreshing needs the admin scope
