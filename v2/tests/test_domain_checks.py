import http.server
import ssl
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine

from backend.adapters.host_helper_client import HostHelperError
from backend.adapters.probes import HttpResult, StdlibHttpProber
from backend.domain.errors import NotFoundError
from backend.repositories.database import UnitOfWork, create_session_factory
from backend.services.domain_checks import CheckProbes, DomainCheckService
from backend.services.domains import DomainDraft, DomainService
from tests.api_support import FakeClock
from tests.conftest import DEV
from tests.ingress_support import HOST, make_certificate
from tests.ingress_world import ACTOR, DEFAULT_IPV4, IngressWorld


class FakeResolver:
    def __init__(self, answers: dict[str, list[str]]) -> None:
        self.answers = answers
        self.asked: list[str] = []

    def resolve(self, hostname: str) -> list[str]:
        self.asked.append(hostname)
        return self.answers.get(hostname, [])


class FakeProber:
    def __init__(self, result: HttpResult) -> None:
        self.result = result
        self.calls: list[tuple[str, bool]] = []

    def probe(self, hostname: str, *, tls: bool, timeout: float) -> HttpResult:
        self.calls.append((hostname, tls))
        return self.result


GOOD = HttpResult(True, status=200, tls_valid=True, not_after=datetime(2027, 1, 1, tzinfo=UTC))


@pytest.fixture
def world(engine: Engine, clock: FakeClock, project: str) -> IngressWorld:
    factory = create_session_factory(engine)
    return IngressWorld(lambda: UnitOfWork(factory), clock)


def checker(world: IngressWorld, resolver: FakeResolver, prober: FakeProber) -> DomainCheckService:
    probes = CheckProbes(resolver, prober, world.helper)
    return DomainCheckService(world.uow_factory(), probes, world.settings, world.clock)


def test_all_checks_pass_and_are_stored_with_a_timestamp(
    world: IngressWorld, clock: FakeClock
) -> None:
    domain = world.create()
    resolver, prober = FakeResolver({HOST: [DEFAULT_IPV4]}), FakeProber(GOOD)
    result = checker(world, resolver, prober).run(domain.id)
    assert result.ok and result.checked_at == clock()
    assert result.results["dns"]["ok"] and result.results["https"]["ok"]  # type: ignore[index]
    assert result.results["backend"]["port"] == 20001  # type: ignore[index]
    assert result.results["https"]["certificate_not_after"].startswith("2027-01-01")  # type: ignore[index]
    assert prober.calls == [(HOST, True)]
    stored = world.domains().get_by_id(domain.id)
    assert stored.last_verified_ok is True and stored.last_verified_at == clock()
    latest = checker(world, resolver, prober).latest(domain.id)
    assert latest is not None and latest.ok and latest.checked_at == clock()
    clock.advance(minutes=5)
    checker(world, resolver, prober).run(domain.id)
    refreshed = checker(world, resolver, prober).latest(domain.id)
    assert refreshed is not None and refreshed.checked_at == clock()  # one row, updated in place


def test_dns_pointing_elsewhere_fails_the_check(world: IngressWorld) -> None:
    domain = world.create()
    result = checker(world, FakeResolver({HOST: ["192.0.2.55"]}), FakeProber(GOOD)).run(domain.id)
    assert not result.ok
    dns = result.results["dns"]
    assert dns["ok"] is False and dns["detail"] == "resolves to other addresses"  # type: ignore[index]
    assert dns["expected"] == [DEFAULT_IPV4]  # type: ignore[index]
    assert result.results["https"]["ok"]  # type: ignore[index]  # parts are independent
    assert world.domains().get_by_id(domain.id).last_verified_ok is False


def test_an_unresolvable_name_fails(world: IngressWorld) -> None:
    domain = world.create()
    result = checker(world, FakeResolver({}), FakeProber(GOOD)).run(domain.id)
    assert result.results["dns"]["detail"] == "the name does not resolve"  # type: ignore[index]


def test_a_proxied_domain_only_needs_to_resolve(world: IngressWorld) -> None:
    domain = world.create(dns_proxied=True)
    result = checker(world, FakeResolver({HOST: ["104.16.0.1"]}), FakeProber(GOOD)).run(domain.id)
    assert result.ok and "proxied" in result.results["dns"]["detail"]  # type: ignore[index]


@pytest.mark.parametrize(
    ("http_result", "fragment"),
    [
        (HttpResult(False, detail="no response: ConnectionRefusedError"), "no response"),
        (
            HttpResult(
                True, tls_valid=False, detail="certificate rejected: certificate has expired"
            ),
            "expired",
        ),
        (HttpResult(True, status=502, tls_valid=True), "HTTP 502"),
    ],
)
def test_https_failures_are_reported_with_a_reason(
    world: IngressWorld, http_result: HttpResult, fragment: str
) -> None:
    domain = world.create()
    result = checker(world, FakeResolver({HOST: [DEFAULT_IPV4]}), FakeProber(http_result)).run(
        domain.id
    )
    https = result.results["https"]
    assert https["ok"] is False and fragment in https["detail"]  # type: ignore[index]
    assert not result.ok


def test_an_application_error_below_500_still_counts_as_responding(world: IngressWorld) -> None:
    domain = world.create()
    ok = HttpResult(True, status=404, tls_valid=True)
    assert checker(world, FakeResolver({HOST: [DEFAULT_IPV4]}), FakeProber(ok)).run(domain.id).ok


def test_without_tls_the_probe_uses_plain_http(world: IngressWorld) -> None:
    domain = world.create(tls_enabled=False)
    prober = FakeProber(HttpResult(True, status=200))
    result = checker(world, FakeResolver({HOST: [DEFAULT_IPV4]}), prober).run(domain.id)
    assert result.ok and prober.calls == [(HOST, False)]


def test_a_backend_that_is_down_or_unprobeable_fails_only_that_part(world: IngressWorld) -> None:
    domain = world.create()
    resolver, prober = FakeResolver({HOST: [DEFAULT_IPV4]}), FakeProber(GOOD)
    world.helper.backend_up = False
    down = checker(world, resolver, prober).run(domain.id)
    assert down.results["backend"]["detail"] == "nothing listens on the published port"  # type: ignore[index]
    assert down.results["dns"]["ok"] and not down.ok  # type: ignore[index]

    def broken(_port: int) -> bool:
        raise HostHelperError("unavailable", "the host helper is not reachable")

    world.helper.check_backend = broken  # type: ignore[method-assign, assignment]
    unreachable = checker(world, resolver, prober).run(domain.id)
    assert "host helper is not reachable" in unreachable.results["backend"]["detail"]  # type: ignore[index]


def test_wildcard_domains_are_probed_through_a_label_under_the_zone(
    world: IngressWorld,
) -> None:
    wildcard_domains = DomainService(world.uow_factory(), world.clock, allow_wildcards=True)
    domain = wildcard_domains.create(
        "demo", DEV, DomainDraft("*.example.com", "web", 8080, 20003), ACTOR
    )
    resolver = FakeResolver({"ingress-check.example.com": [DEFAULT_IPV4]})
    result = checker(world, resolver, FakeProber(GOOD)).run(domain.id)
    assert result.ok and resolver.asked == ["ingress-check.example.com"]


def test_unknown_domain(world: IngressWorld) -> None:
    with pytest.raises(NotFoundError):
        checker(world, FakeResolver({}), FakeProber(GOOD)).run(999)
    assert checker(world, FakeResolver({}), FakeProber(GOOD)).latest(999) is None


# --- the real prober against a local TLS server ------------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_args: object) -> None:
        pass


def test_the_real_prober_rejects_a_certificate_it_does_not_trust(tmp_path: Path) -> None:
    now = datetime.now(tz=UTC)
    chain, key = make_certificate("localhost", now - timedelta(days=1), now + timedelta(days=5))
    cert_file, key_file = tmp_path / "c.pem", tmp_path / "k.pem"
    cert_file.write_text(chain)
    key_file.write_text(key)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # A self-signed certificate is not trusted by the default context: must not be "valid".
        result = StdlibHttpProber().probe(f"localhost:{server.server_port}", tls=True, timeout=5)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
    assert result.reached and result.tls_valid is False
    assert "certificate rejected" in result.detail


def test_the_real_prober_reports_a_closed_port_without_raising() -> None:
    result = StdlibHttpProber().probe("127.0.0.1:1", tls=False, timeout=2)
    assert not result.reached and "no response" in result.detail
