"""Fakes and builders shared by the ingress tests (no network, no Cloudflare, no real CA)."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from backend.adapters.acme.issuer import IssuedCertificate
from backend.adapters.cloudflare import CloudflareClient, CloudflareDns
from backend.adapters.host_helper_client import (
    HostHelperError,
    InstalledCertificate,
    VhostDiscovery,
    VhostEntry,
    VhostProposal,
)
from backend.hosthelper.vhost import VhostPaths, VhostSpec, render_vhost
from backend.repositories.models import Domain

FIXTURES = Path(__file__).parent / "fixtures"
ZONE = "example.com"
HOST = "app.example.com"


def fixture(*parts: str) -> Any:
    """Load a JSON fixture below ``tests/fixtures``."""
    return json.loads(Path(FIXTURES, *parts).read_text(encoding="utf-8"))


def make_certificate(
    hostname: str,
    not_before: datetime,
    not_after: datetime,
    *,
    san: list[str] | None = None,
) -> tuple[str, str]:
    """Return ``(fullchain_pem, private_key_pem)`` of a self-signed test certificate."""
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname[:64])])
    names = [x509.DNSName(item) for item in (san or [hostname])]
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.SubjectAlternativeName(names), critical=False)
        .sign(key, hashes.SHA256())
    )
    chain = cert.public_bytes(serialization.Encoding.PEM).decode()
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return chain, private


class FakeHelper:
    """In-memory ``HostHelper`` that renders with the real template and records every call."""

    def __init__(self) -> None:
        self.paths = VhostPaths(Path("/certs"), Path("/webroot"))
        self.vhosts: dict[str, str] = {}
        self.certificates: dict[str, str] = {}
        self.calls: list[str] = []
        self.backend_up = True
        self.fail_put: HostHelperError | None = None
        self.challenges: dict[str, str] = {}
        # What ``vhost.discover`` reports; ``discover_error`` scripts "helper missing/too old".
        self.existing_sites: list[VhostEntry] = []
        self.discover_error: HostHelperError | None = None

    def discover_vhosts(self) -> VhostDiscovery:
        """The scripted Apache sites (or the scripted failure)."""
        self.calls.append("discover_vhosts")
        if self.discover_error is not None:
            raise self.discover_error
        return VhostDiscovery(tuple(self.existing_sites))

    def render_vhost(
        self, hostname: str, upstream_port: int, *, tls: bool, websocket: bool = True
    ) -> VhostProposal:
        """Render without storing."""
        text = render_vhost(VhostSpec(hostname, upstream_port, tls, websocket), self.paths)
        return VhostProposal(text, self.vhosts.get(hostname))

    def get_vhost(self, hostname: str) -> str | None:
        """Return the stored vhost."""
        return self.vhosts.get(hostname)

    def put_vhost(
        self, hostname: str, upstream_port: int, *, tls: bool, websocket: bool = True
    ) -> bool:
        """Store the vhost (unless a failure was scripted)."""
        self.calls.append(f"put_vhost tls={tls}")
        if self.fail_put is not None:
            raise self.fail_put
        text = render_vhost(VhostSpec(hostname, upstream_port, tls, websocket), self.paths)
        changed = self.vhosts.get(hostname) != text
        self.vhosts[hostname] = text
        return changed

    def remove_vhost(self, hostname: str) -> bool:
        """Delete the stored vhost."""
        self.calls.append("remove_vhost")
        return self.vhosts.pop(hostname, None) is not None

    def install_certificate(self, hostname: str, fullchain_pem: str, private_key_pem: str) -> None:
        """Store the certificate chain (the key is never kept)."""
        assert "PRIVATE KEY" in private_key_pem
        self.calls.append("install_certificate")
        self.certificates[hostname] = fullchain_pem

    def certificate_info(self, hostname: str) -> InstalledCertificate | None:
        """Report the stored certificate."""
        chain = self.certificates.get(hostname)
        if chain is None:
            return None
        leaf = x509.load_pem_x509_certificate(chain.encode())
        return InstalledCertificate(leaf.not_valid_after_utc, f"{leaf.serial_number:x}")

    def remove_certificate(self, hostname: str) -> bool:
        """Delete the stored certificate."""
        self.calls.append("remove_certificate")
        return self.certificates.pop(hostname, None) is not None

    def put_challenge(self, token: str, content: str) -> None:
        """Remember an HTTP-01 file."""
        self.challenges[token] = content

    def remove_challenge(self, token: str) -> None:
        """Forget an HTTP-01 file."""
        self.challenges.pop(token, None)

    def check_backend(self, port: int) -> bool:
        """Report the scripted backend state."""
        return self.backend_up


@dataclass
class FakeIssuer:
    """``CertificateIssuer`` that signs nothing real: a self-signed cert valid ``days`` days."""

    clock: Callable[[], datetime]
    days: int = 90
    challenge_type: str = "dns-01"
    fail_with: Exception | None = None
    issued: list[str] = field(default_factory=list)

    def issue(self, hostname: str) -> IssuedCertificate:
        """Issue (or fail on demand)."""
        if self.fail_with is not None:
            raise self.fail_with
        self.issued.append(hostname)
        now = self.clock().astimezone(UTC)
        chain, key = make_certificate(
            hostname, now - timedelta(minutes=1), now + timedelta(days=self.days)
        )
        return IssuedCertificate(chain, key)


class FakeCloudflare:
    """A stateful stand-in for the Cloudflare API behind ``httpx.MockTransport``.

    Response bodies are built from the JSON fixtures in ``tests/fixtures/cloudflare`` so the
    wire shapes match Cloudflare's documented v4 format. Every request is recorded.
    """

    def __init__(self, token: str = "cf-test-token") -> None:  # noqa: S107
        self.token = token
        self.zones = fixture("cloudflare", "zones.json")
        self.records: list[dict[str, Any]] = []
        self.requests: list[tuple[str, str]] = []
        self.next_id = 1
        self.fail_create_once_with: dict[str, Any] | None = None
        self.reject_auth = False
        self.account_token = False

    def add_record(self, **fields: Any) -> dict[str, Any]:
        """Seed a record (Cloudflare field names)."""
        record = {"id": f"rec{self.next_id}", "ttl": 1, "proxied": False, "comment": None}
        record.update(fields)
        self.next_id += 1
        self.records.append(record)
        return record

    def client(self) -> CloudflareClient:
        """A real ``CloudflareClient`` talking to this fake."""
        http = httpx.Client(transport=httpx.MockTransport(self.handle))
        return CloudflareClient(self.token, base_url="https://api.test/client/v4", client=http)

    def dns(self) -> CloudflareDns:
        """A real ``CloudflareDns`` on top of ``client``."""
        return CloudflareDns(self.client())

    def handle(self, request: httpx.Request) -> httpx.Response:  # noqa: PLR0911
        """Serve one request."""
        self.requests.append((request.method, request.url.path))
        if self.reject_auth or request.headers.get("authorization") != f"Bearer {self.token}":
            return httpx.Response(401, json=fixture("cloudflare", "error_auth.json"))
        path = request.url.path.removeprefix("/client/v4")
        if request.method == "GET" and path == "/user/tokens/verify":
            if self.account_token:
                return httpx.Response(401, json=fixture("cloudflare", "error_auth.json"))
            return httpx.Response(200, json={"success": True, "result": {"status": "active"}})
        if request.method == "GET" and path == "/accounts":
            return httpx.Response(200, json={"success": True, "result": [{"id": "account-1"}]})
        if request.method == "GET" and path == "/accounts/account-1/tokens/verify":
            return httpx.Response(200, json={"success": True, "result": {"status": "active"}})
        if request.method == "GET" and path == "/zones":
            return self._zones(request)
        parts = path.strip("/").split("/")
        if len(parts) >= 3 and parts[2] == "dns_records":
            return self._records(request, parts)
        return httpx.Response(404, json=fixture("cloudflare", "error_notfound.json"))

    def _zones(self, request: httpx.Request) -> httpx.Response:
        name = request.url.params.get("name")
        result = [z for z in self.zones["result"] if name is None or z["name"] == name]
        return httpx.Response(200, json={**self.zones, "result": result})

    def _records(self, request: httpx.Request, parts: list[str]) -> httpx.Response:
        template = fixture("cloudflare", "records_page.json")
        if request.method == "GET":
            name = request.url.params.get("name")
            result = [r for r in self.records if r["name"] == name]
            return httpx.Response(200, json={**template, "result": result})
        if request.method == "POST":
            if self.fail_create_once_with is not None:
                error, self.fail_create_once_with = self.fail_create_once_with, None
                return httpx.Response(400, json=error)
            record = self.add_record(**json.loads(request.content))
            return httpx.Response(200, json={"success": True, "errors": [], "result": record})
        record = next(r for r in self.records if r["id"] == parts[3])
        if request.method == "PUT":
            record.update(json.loads(request.content))
            return httpx.Response(200, json={"success": True, "errors": [], "result": record})
        self.records.remove(record)
        return httpx.Response(200, json={"success": True, "result": {"id": record["id"]}})

    def mutations(self) -> list[tuple[str, str]]:
        """Requests that were not reads."""
        return [item for item in self.requests if item[0] != "GET"]


def domain_stub(**fields: Any) -> Domain:
    """An unsaved ``Domain`` for tests that do not need the database."""
    values: dict[str, Any] = {"id": 1, "environment_id": 1, "hostname": HOST}
    values.update(fields)
    return Domain(**values)
