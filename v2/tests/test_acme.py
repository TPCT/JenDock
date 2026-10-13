import stat
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from shiftcodes.adapters.acme.client import AcmeClient, AcmeConfig, AcmeError
from shiftcodes.adapters.acme.issuer import AcmeIssuer
from shiftcodes.adapters.acme.jws import AccountKey, load_or_create_account_key
from shiftcodes.adapters.acme.solvers import (
    Dns01CloudflareSolver,
    Http01HelperSolver,
    dns01_value,
)
from shiftcodes.domain.ingress import MANAGED_TAG
from tests.fake_acme import BASE, FakeAcmeCa
from tests.ingress_support import HOST, FakeCloudflare, FakeHelper

CONFIG = AcmeConfig(f"{BASE}/directory", email="ops@example.com", poll_timeout=30, poll_interval=1)


class Clock:
    """A monotonic clock whose ``sleep`` advances it."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_client(ca: FakeAcmeCa, clock: Clock, key: AccountKey | None = None) -> AcmeClient:
    key = key or AccountKey(ec.generate_private_key(ec.SECP256R1()))
    http = httpx.Client(transport=ca.transport())
    return AcmeClient(CONFIG, key, http, sleep=clock.sleep, monotonic=clock.monotonic)


class DnsEnv:
    """CA + Cloudflare fake + dns-01 solver; the CA validates by reading the fake zone."""

    def __init__(self) -> None:
        self.cloudflare = FakeCloudflare()
        self.clock = Clock()
        self.ca = FakeAcmeCa(self._verify, challenge_types=("dns-01",))
        self.solver = Dns01CloudflareSolver(
            self.cloudflare.client(), propagation_wait=0, sleep=lambda _s: None
        )
        self.seen_at_validation: list[dict[str, object]] = []

    def _verify(self, kind: str, domain: str, _token: str, key_authorization: str) -> bool:
        assert kind == "dns-01"
        name = f"_acme-challenge.{domain}"
        value = dns01_value(key_authorization)
        records = [r for r in self.cloudflare.records if r["name"] == name]
        self.seen_at_validation.extend(records)
        return any(r["type"] == "TXT" and r["content"] == value for r in records)


def issue(env: DnsEnv, hostname: str = HOST) -> str:
    issuer = AcmeIssuer(make_client(env.ca, env.clock), env.solver)
    return issuer.issue(hostname).fullchain_pem


def test_dns01_issuance_end_to_end_with_signed_requests() -> None:
    env = DnsEnv()
    issued = AcmeIssuer(make_client(env.ca, env.clock), env.solver).issue(HOST)
    leaf = x509.load_pem_x509_certificates(issued.fullchain_pem.encode())[0]
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == [HOST]
    key = serialization.load_pem_private_key(issued.private_key_pem.encode(), password=None)
    assert key.public_key().public_numbers() == leaf.public_key().public_numbers()  # type: ignore[union-attr]
    assert env.ca.new_account_calls == 1
    # The TXT proof existed during validation, was tagged as ours, and is gone afterwards.
    assert env.seen_at_validation
    assert str(env.seen_at_validation[0]["comment"]).startswith(MANAGED_TAG)
    assert [r for r in env.cloudflare.records if r["type"] == "TXT"] == []
    assert env.clock.sleeps  # it polled with the injected sleep, not real time


def test_dns01_cleanup_never_removes_foreign_txt_records() -> None:
    env = DnsEnv()
    foreign = env.cloudflare.add_record(
        type="TXT", name=f"_acme-challenge.{HOST}", content="someone-elses-token", comment=None
    )
    mine_but_other_value = env.cloudflare.add_record(
        type="TXT",
        name=f"_acme-challenge.{HOST}",
        content="stale-own-value",
        comment=f"{MANAGED_TAG} purpose=acme-dns-01",
    )
    issue(env)
    assert foreign in env.cloudflare.records
    assert mine_but_other_value in env.cloudflare.records  # only the exact value is cleaned up


def test_a_failed_validation_raises_and_still_cleans_up() -> None:
    env = DnsEnv()
    env.solver = Dns01CloudflareSolver(
        env.cloudflare.client(), propagation_wait=0, sleep=lambda _s: None
    )
    env.ca.verifier = lambda *_args: False
    with pytest.raises(AcmeError, match="the proof was not found") as raised:
        issue(env)
    assert raised.value.problem == "unauthorized"
    assert [r for r in env.cloudflare.records if r["type"] == "TXT"] == []


def test_a_bad_nonce_is_retried_transparently() -> None:
    env = DnsEnv()
    env.ca.bad_nonce_once = True
    assert "BEGIN CERTIFICATE" in issue(env)


def test_waiting_for_the_ca_times_out() -> None:
    env = DnsEnv()
    env.ca.pending_polls = 10_000
    with pytest.raises(AcmeError, match="timed out"):
        issue(env)
    assert [r for r in env.cloudflare.records if r["type"] == "TXT"] == []


def test_a_ca_without_the_requested_challenge_type_is_reported() -> None:
    env = DnsEnv()
    env.ca = FakeAcmeCa(env._verify, challenge_types=("http-01",))
    with pytest.raises(AcmeError, match="no dns-01 challenge"):
        issue(env)


def test_the_http01_solver_publishes_and_removes_the_challenge_file() -> None:
    helper = FakeHelper()
    seen: list[dict[str, str]] = []

    def verify(kind: str, _domain: str, token: str, key_authorization: str) -> bool:
        seen.append(dict(helper.challenges))
        return kind == "http-01" and helper.challenges.get(token) == key_authorization

    ca = FakeAcmeCa(verify, challenge_types=("http-01",))
    issuer = AcmeIssuer(make_client(ca, Clock()), Http01HelperSolver(helper))
    assert issuer.challenge_type == "http-01"
    assert "BEGIN CERTIFICATE" in issuer.issue(HOST).fullchain_pem
    assert seen and len(seen[0]) == 1
    assert helper.challenges == {}


def test_a_ca_error_surfaces_its_detail_without_key_material() -> None:
    env = DnsEnv()

    env.ca = FakeAcmeCa(env._verify, challenge_types=("dns-01",))
    transport = env.ca.transport()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/new-order":
            return httpx.Response(
                403,
                json={
                    "type": "urn:ietf:params:acme:error:rejectedIdentifier",
                    "detail": "not allowed",
                },
            )
        return transport.handle_request(request)

    client = AcmeClient(
        CONFIG,
        AccountKey(ec.generate_private_key(ec.SECP256R1())),
        httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _s: None,
    )
    with pytest.raises(AcmeError, match="not allowed") as raised:
        AcmeIssuer(client, env.solver).issue(HOST)
    assert raised.value.problem == "rejectedIdentifier"
    assert "PRIVATE KEY" not in str(raised.value)


def test_each_issuance_uses_a_fresh_private_key() -> None:
    env = DnsEnv()
    first = AcmeIssuer(make_client(env.ca, env.clock), env.solver).issue(HOST)
    second = AcmeIssuer(make_client(env.ca, env.clock), env.solver).issue(HOST)
    assert first.private_key_pem != second.private_key_pem


def test_account_key_is_created_private_and_reloaded(tmp_path: Path) -> None:
    path = tmp_path / "private" / "acme.key"
    first = AccountKey(load_or_create_account_key(path))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    second = AccountKey(load_or_create_account_key(path))
    assert first.thumbprint == second.thumbprint


def test_dns01_value_is_the_base64url_sha256_of_the_key_authorization() -> None:
    # sha256("abc") = ba7816bf...; base64url without padding
    assert dns01_value("abc") == "ungWv48Bz-pBQUDeXa4iI7ADYaOWF3qctBD_YfIAFa0"


def test_key_authorization_is_token_dot_thumbprint() -> None:
    key = AccountKey(ec.generate_private_key(ec.SECP256R1()))
    assert key.key_authorization("tok") == f"tok.{key.thumbprint}"
    assert len(key.thumbprint) == 43
