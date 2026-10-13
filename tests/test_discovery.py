"""Discovery: Cloudflare paging, Apache facts, matching and the snapshot (all fakes, no network)."""

from __future__ import annotations

import pytest
from sqlalchemy import Engine

from backend.adapters.cloudflare import CloudflareError
from backend.adapters.host_helper_client import HostHelperError, VhostEntry
from backend.domain.actor import Actor
from backend.domain.discovery import (
    Confidence,
    DnsFact,
    IpSource,
    SourceState,
    VhostFact,
    is_cloudflare_edge,
    points_to_server,
    suggest,
)
from backend.domain.enums import EnvironmentName
from backend.repositories.database import UnitOfWork, create_session_factory
from backend.repositories.models import DiscoveredDomain
from backend.services.discovery import DiscoveryService
from backend.services.discovery_queries import DiscoveryQueryService, DiscoveryStateView
from tests.discovery_support import (
    SERVER_IP,
    FakePorts,
    PagedCloudflare,
    PortsBuilder,
    project_ref,
    published,
)
from tests.ingress_support import FakeHelper

ACTOR = Actor.system("test")
PROD = EnvironmentName.PRODUCTION


def site(
    file: str,
    names: list[str],
    port: int | None = None,
    *,
    ssl: bool = False,
    managed: bool = False,
) -> VhostEntry:
    return VhostEntry(
        file,
        managed,
        tuple(names),
        (443,) if ssl else (80,),
        "127.0.0.1" if port else None,
        port,
        ssl,
    )


class World:
    def __init__(self, engine: Engine) -> None:
        self.factory = create_session_factory(engine)
        self.cloudflare = PagedCloudflare()
        self.helper = FakeHelper()
        self.catalog = FakePorts(
            [project_ref("shop-api"), project_ref("marketing-site")],
            [published("shop-api", "web", 8080, 20001)],
        )
        self.ports = PortsBuilder(self.cloudflare, self.helper, self.catalog)

    def refresh(self) -> None:
        DiscoveryService(UnitOfWork(self.factory), self.ports.build()).refresh(ACTOR)

    def state(self) -> DiscoveryStateView:
        return DiscoveryQueryService(UnitOfWork(self.factory)).state()

    def rows(self) -> dict[str, DiscoveredDomain]:
        with UnitOfWork(self.factory) as uow:
            return {row.hostname: row for row in uow.discovery.list_all()}


@pytest.fixture
def world(engine: Engine) -> World:
    w = World(engine)
    zone = w.cloudflare.add_zone("example.com")
    w.cloudflare.add_zone("example.org")
    w.cloudflare.add_record("example.com", "A", "shop.example.com", SERVER_IP, proxied=True)
    w.cloudflare.add_record("example.com", "A", "shop-api.example.com", SERVER_IP)
    w.cloudflare.add_record("example.com", "A", "other.example.com", "198.51.100.9")
    w.cloudflare.add_record("example.com", "CNAME", "www.example.com", "shop.example.com")
    w.cloudflare.add_record("example.com", "CNAME", "elsewhere.example.com", "x.netlify.app")
    w.cloudflare.add_record("example.org", "AAAA", "v6.example.org", "2001:db8::1")
    assert zone
    return w


def test_cloudflare_is_read_page_by_page_and_only_read(world: World) -> None:
    world.refresh()
    rows = world.rows()
    # only records that lead to the server are listed: other.* and elsewhere.* are not ours
    assert set(rows) == {"shop.example.com", "shop-api.example.com", "www.example.com"}
    assert world.cloudflare.mutations() == []
    zone_calls = [r for r in world.cloudflare.requests if r.url.path.endswith("/zones")]
    assert zone_calls and all(r.url.params["per_page"] == "50" for r in zone_calls)
    assert any(r.url.params.get("page") == "2" for r in world.cloudflare.requests)


def test_cname_to_a_server_record_points_here_and_proxy_flag_is_kept(world: World) -> None:
    world.refresh()
    rows = world.rows()
    assert rows["www.example.com"].points_to_server is True
    assert rows["shop.example.com"].proxied is True
    assert world.state().zones == ("example.com", "example.org")


def test_apache_sites_are_merged_and_matched_by_upstream_port(world: World) -> None:
    world.helper.existing_sites = [
        site("shop.conf", ["shop.example.com"], None),  # :80 redirect block
        site("shop-ssl.conf", ["shop.example.com", "www.example.com"], 20001, ssl=True),
        site("old.conf", ["legacy.example.com"], 3000),  # Apache only, no DNS record
    ]
    world.refresh()
    rows = world.rows()
    shop = rows["shop.example.com"]
    assert (shop.upstream_port, shop.vhost_ssl, shop.vhost_file) == (
        20001,
        True,
        "shop-ssl.conf",
    )
    assert shop.suggested_project == "shop-api"
    assert shop.suggestion_confidence is Confidence.HIGH
    assert shop.suggested_service == "web" and shop.suggested_container_port == 8080
    legacy = rows["legacy.example.com"]
    assert legacy.zone_name == "example.com" and legacy.record_type is None
    state = world.state()
    assert state.helper is SourceState.OK and state.cloudflare is SourceState.OK
    assert state.adoptable == 2  # shop.example.com and www.example.com share the port


def test_without_the_helper_cloudflare_alone_still_works(world: World) -> None:
    world.helper.discover_error = HostHelperError("unavailable", "the host helper is not reachable")
    world.refresh()
    state = world.state()
    assert state.helper is SourceState.MISSING and state.cloudflare is SourceState.OK
    assert len(world.rows()) == 3
    # name-based suggestion survives without Apache
    assert world.rows()["shop-api.example.com"].suggested_project == "shop-api"


def test_an_old_helper_is_reported_as_outdated(world: World) -> None:
    world.helper.discover_error = HostHelperError("invalid_request", "unknown operation")
    world.refresh()
    assert world.state().helper is SourceState.OUTDATED


def test_no_token_keeps_apache_rows_and_says_so(world: World) -> None:
    world.ports.token = None
    world.helper.existing_sites = [site("a.conf", ["a.example.com"], 3000)]
    world.refresh()
    state = world.state()
    assert state.cloudflare is SourceState.NO_TOKEN
    assert set(world.rows()) == {"a.example.com"}
    assert world.cloudflare.requests == []


def test_a_cloudflare_failure_keeps_the_previous_snapshot(world: World) -> None:
    world.refresh()
    before = set(world.rows())
    world.cloudflare.fail_with = 500
    world.refresh()
    assert set(world.rows()) == before
    state = world.state()
    assert state.cloudflare is SourceState.ERROR and state.cloudflare_message


def test_server_ip_falls_back_to_the_public_origin_but_never_to_cloudflare_edges(
    world: World,
) -> None:
    world.ports.settings = world.ports.settings.model_copy(update={"public_ipv4": None})
    world.ports.resolved = {"jendock.example.test": ["104.16.1.1"]}  # a Cloudflare edge address
    world.refresh()
    assert world.state().ip_source is IpSource.UNKNOWN
    assert len(world.rows()) == 6  # unknown address: nothing can be excluded
    world.ports.resolved = {"jendock.example.test": [SERVER_IP]}
    world.refresh()
    state = world.state()
    assert (state.ip_source, state.server_ip) == (IpSource.ORIGIN, SERVER_IP)


def test_zone_and_lookup_use_the_token(world: World) -> None:
    service = DiscoveryService(UnitOfWork(world.factory), world.ports.build())
    assert service.zones().zones == ("example.com", "example.org")
    found = service.lookup("shop.example.com")
    assert found.zone == "example.com" and found.records[0].content == SERVER_IP
    assert service.lookup("free.example.com").records == ()
    world.ports.token = None
    assert service.zones().configured is False
    assert service.lookup("shop.example.com").configured is False


def test_a_rejected_token_is_an_error_not_a_crash() -> None:
    fake = PagedCloudflare(token="right")
    bad = PagedCloudflare(token="right").client()
    bad._token = "wrong"
    with pytest.raises(CloudflareError):
        bad.list_zones()
    assert fake.client().list_zones() == []


# --- pure matching ---------------------------------------------------------------------------


def test_port_match_is_high_and_ignores_the_name() -> None:
    ports = [published("shop-api", "web", 8080, 20001)]
    found = suggest(
        "totally-unrelated.example.com",
        VhostFact("a.conf", "x", "127.0.0.1", 20001, True),
        ports,
        [project_ref("shop-api")],
    )
    assert found is not None and found.confidence is Confidence.HIGH and found.complete
    assert (found.project, found.environment, found.service) == ("shop-api", PROD, "web")


def test_a_port_shared_by_two_containers_is_not_guessed() -> None:
    ports = [published("a", "web", 80, 20001), published("b", "web", 80, 20001, "development")]
    assert (
        suggest("x.example.com", VhostFact("f", "x", "127.0.0.1", 20001, False), ports, []) is None
    )


def test_non_local_upstreams_never_match_ports() -> None:
    ports = [published("shop-api", "web", 8080, 20001)]
    remote = VhostFact("f", "x", "10.0.0.5", 20001, False)
    assert suggest("x.example.com", remote, ports, []) is None


def test_name_match_is_medium_incomplete_and_guesses_the_environment() -> None:
    projects = [project_ref("shop-api"), project_ref("billing")]
    found = suggest("dev.shop-api.example.com", None, [], projects)
    assert found is not None and not found.complete
    assert found.confidence is Confidence.MEDIUM
    assert found.environment is EnvironmentName.DEVELOPMENT
    assert suggest("workshop.example.com", None, [], [project_ref("shop")]) is None


def test_ambiguous_names_give_no_suggestion() -> None:
    projects = [project_ref("shop-api"), project_ref("shop-web")]
    assert suggest("shop.example.com", None, [], projects) is None  # both slugs match
    assert suggest("zzz.example.com", None, [], projects) is None


def test_cname_chain_and_loops() -> None:
    def fact(name: str, kind: str, content: str) -> DnsFact:
        return DnsFact("z", "example.com", name, kind, name, content, False)

    a = fact("a.example.com", "A", SERVER_IP)
    b = fact("b.example.com", "CNAME", "a.example.com")
    c = fact("c.example.com", "CNAME", "b.example.com")
    loop = fact("l.example.com", "CNAME", "l.example.com")
    everything = [a, b, c, loop]
    ips = frozenset({SERVER_IP})
    assert points_to_server(c, everything, ips) is True
    assert points_to_server(loop, everything, ips) is False
    assert points_to_server(a, everything, frozenset()) is None


def test_cloudflare_edge_ranges() -> None:
    assert is_cloudflare_edge("104.16.5.5") and is_cloudflare_edge("2606:4700::1")
    assert not is_cloudflare_edge(SERVER_IP) and not is_cloudflare_edge("not an ip")
