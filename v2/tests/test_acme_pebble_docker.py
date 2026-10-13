"""The ACME client against Pebble, Let's Encrypt's test CA, with a challenge test server.

Run with ``make test-docker``. Needs the images ``ghcr.io/letsencrypt/pebble`` and
``pebble-challtestsrv`` locally (pinned by digest below; nothing is pulled). A throw-away
Docker network and two containers are created, all labelled ``shiftcodes.test=1`` plus a label
unique to this module, and removed afterwards. Pebble validates DNS-01 against the challenge
test server's DNS and HTTP-01 against its HTTP server, so both solver *protocols* are
exercised for real; the Cloudflare and helper side of the solvers is covered by unit tests.
"""

from __future__ import annotations

import json
import shutil
import ssl
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec

from shiftcodes.adapters.acme.client import AcmeClient, AcmeConfig, AcmeError
from shiftcodes.adapters.acme.issuer import AcmeIssuer
from shiftcodes.adapters.acme.jws import AccountKey
from shiftcodes.adapters.acme.solvers import dns01_value
from shiftcodes.hosthelper.certs import validate_material
from tests.docker_support import TEST_LABEL, docker_available

PEBBLE = (
    "ghcr.io/letsencrypt/pebble"
    "@sha256:5080492187bea12909c053d7a2b90a9e95104ef1c82381828e7823db71ce7e54"
)
CHALLTESTSRV = (
    "ghcr.io/letsencrypt/pebble-challtestsrv"
    "@sha256:1994a326ec60e0de5e8051aa192269c1aaeff0969a4b703d11fa51370f3dc1a8"
)
OWN = f"shiftcodes.test.owner=pebble-{uuid.uuid4().hex[:12]}"
DOCKER = shutil.which("docker") or "docker"


def _docker(*args: str, timeout: float = 60) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [DOCKER, *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def _images_present() -> bool:
    if not docker_available():
        return False
    return all(
        _docker("image", "inspect", image).returncode == 0 for image in (PEBBLE, CHALLTESTSRV)
    )


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not docker_available(), reason="no Docker daemon available"),
    pytest.mark.skipif(not _images_present(), reason="pebble images are not available locally"),
]


@dataclass
class PebbleEnv:
    directory_url: str
    ca_bundle: Path
    management: str  # base URL of the challenge test server's management API


def _context(bundle: Path) -> ssl.SSLContext:
    return ssl.create_default_context(cafile=str(bundle))


def _published(container: str, port: int) -> str:
    result = _docker("port", container, f"{port}/tcp")
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()[0].rsplit(":", 1)[1]


def _run(
    name: str, network: str, image: str, *args: str, publish: list[int], env: list[str]
) -> None:
    command = [
        "run", "--detach", "--name", name, "--network", network, "--label", TEST_LABEL,
        "--label", OWN,
    ]  # fmt: skip
    for port in publish:
        command += ["--publish", f"127.0.0.1::{port}"]
    for item in env:
        command += ["--env", item]
    result = _docker(*command, image, *args)
    assert result.returncode == 0, result.stderr


def _wait_until(check, what: str, timeout: float = 60) -> None:  # type: ignore[no-untyped-def]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except (httpx.HTTPError, OSError):
            pass
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {what}")


# Function scope on purpose: other docker tests remove every container labelled
# shiftcodes.test=1 in their teardown, which would kill a module-wide Pebble.
@pytest.fixture
def pebble(tmp_path_factory: pytest.TempPathFactory) -> Iterator[PebbleEnv]:
    suffix = uuid.uuid4().hex[:8]
    network, srv, ca = f"sc-pebble-net-{suffix}", f"sc-challtestsrv-{suffix}", f"sc-pebble-{suffix}"
    created = _docker("network", "create", "--label", TEST_LABEL, "--label", OWN, network)
    assert created.returncode == 0, created.stderr
    try:
        _run(srv, network, CHALLTESTSRV, publish=[8055], env=[])
        _run(
            ca,
            network,
            PEBBLE,
            "-config", "/test/config/pebble-config.json", "-dnsserver", f"{srv}:8053",
            publish=[14000],
            env=["PEBBLE_VA_NOSLEEP=1", "PEBBLE_WFE_NONCEREJECT=0"],
        )  # fmt: skip
        bundle = tmp_path_factory.mktemp("pebble") / "minica.pem"
        copied = _docker("cp", f"{ca}:/test/certs/pebble.minica.pem", str(bundle))
        assert copied.returncode == 0, copied.stderr
        management = f"http://127.0.0.1:{_published(srv, 8055)}"
        directory = f"https://localhost:{_published(ca, 14000)}/dir"
        inspected = json.loads(_docker("inspect", srv).stdout)
        address = inspected[0]["NetworkSettings"]["Networks"][network]["IPAddress"]
        assert address
        _wait_until(
            lambda: (
                httpx.post(f"{management}/set-default-ipv4", json={"ip": address}).status_code
                == 200
            ),
            "the challenge test server",
        )
        # No AAAA answer: Pebble would otherwise try ::1 inside its own container first.
        httpx.post(f"{management}/set-default-ipv6", json={"ip": ""}).raise_for_status()
        _wait_until(
            lambda: httpx.get(directory, verify=_context(bundle)).status_code == 200, "pebble"
        )
        yield PebbleEnv(directory, bundle, management)
    finally:
        _docker("rm", "--force", srv, ca)
        _docker("network", "rm", network)
        leftovers = _docker("ps", "--all", "--quiet", "--filter", f"label={OWN}").stdout.split()
        networks = _docker("network", "ls", "--quiet", "--filter", f"label={OWN}").stdout.split()
        assert leftovers == [] and networks == [], "pebble test resources remain"


class ChallTestSrvSolver:
    """Publishes proofs at the challenge test server (DNS TXT or HTTP-01 content)."""

    def __init__(self, env: PebbleEnv, challenge_type: str) -> None:
        self.challenge_type = challenge_type
        self._management = env.management
        self.presented: list[str] = []

    def present(self, domain: str, token: str, key_authorization: str) -> None:
        if self.challenge_type == "dns-01":
            body = {"host": f"_acme-challenge.{domain}.", "value": dns01_value(key_authorization)}
            httpx.post(f"{self._management}/set-txt", json=body).raise_for_status()
        else:
            body = {"token": token, "content": key_authorization}
            httpx.post(f"{self._management}/add-http01", json=body).raise_for_status()
        self.presented.append(domain)

    def cleanup(self, domain: str, token: str, key_authorization: str) -> None:
        if self.challenge_type == "dns-01":
            body = {"host": f"_acme-challenge.{domain}."}
            httpx.post(f"{self._management}/clear-txt", json=body).raise_for_status()
        else:
            httpx.post(f"{self._management}/del-http01", json={"token": token}).raise_for_status()


def issue(
    env: PebbleEnv, hostname: str, challenge_type: str
) -> tuple[str, str, ChallTestSrvSolver]:
    solver = ChallTestSrvSolver(env, challenge_type)
    http = httpx.Client(verify=_context(env.ca_bundle), timeout=30)
    config = AcmeConfig(
        env.directory_url, email="ops@example.org", poll_timeout=60, poll_interval=1
    )
    client = AcmeClient(config, AccountKey(ec.generate_private_key(ec.SECP256R1())), http)
    issued = AcmeIssuer(client, solver).issue(hostname)
    http.close()
    return issued.fullchain_pem, issued.private_key_pem, solver


@pytest.mark.parametrize("challenge_type", ["dns-01", "http-01"])
def test_pebble_issues_a_certificate_for_each_challenge_type(
    pebble: PebbleEnv, challenge_type: str
) -> None:
    hostname = f"app-{uuid.uuid4().hex[:6]}.example.org"
    chain, key, solver = issue(pebble, hostname, challenge_type)
    assert solver.presented == [hostname]
    leaf = x509.load_pem_x509_certificates(chain.encode())[0]
    san = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.DNSName) == [hostname]
    assert "Pebble" in leaf.issuer.rfc4514_string()
    assert leaf.not_valid_after_utc > datetime.now(UTC) + timedelta(days=1)
    # What the client produces is accepted by the host helper's own validation, key and all.
    info = validate_material(hostname, chain, key)
    assert info.not_after == leaf.not_valid_after_utc


def test_pebble_renewal_issues_a_new_certificate_with_a_new_key(pebble: PebbleEnv) -> None:
    hostname = f"renew-{uuid.uuid4().hex[:6]}.example.org"
    first_chain, first_key, _ = issue(pebble, hostname, "dns-01")
    second_chain, second_key, _ = issue(pebble, hostname, "dns-01")
    assert first_key != second_key
    first = x509.load_pem_x509_certificates(first_chain.encode())[0]
    second = x509.load_pem_x509_certificates(second_chain.encode())[0]
    assert first.serial_number != second.serial_number


def test_pebble_rejects_a_missing_proof_and_reports_the_ca_error(pebble: PebbleEnv) -> None:
    class Silent(ChallTestSrvSolver):
        def present(self, domain: str, token: str, key_authorization: str) -> None:
            self.presented.append(domain)  # publishes nothing

    http = httpx.Client(verify=_context(pebble.ca_bundle), timeout=30)
    config = AcmeConfig(pebble.directory_url, poll_timeout=60, poll_interval=1)
    client = AcmeClient(config, AccountKey(ec.generate_private_key(ec.SECP256R1())), http)
    with pytest.raises(AcmeError) as raised:
        AcmeIssuer(client, Silent(pebble, "dns-01")).issue(
            f"none-{uuid.uuid4().hex[:6]}.example.org"
        )
    assert "challenge failed" in str(raised.value) or "authorization" in str(raised.value)
    http.close()


def test_the_pebble_directory_is_what_the_client_uses(pebble: PebbleEnv) -> None:
    data = json.loads(httpx.get(pebble.directory_url, verify=_context(pebble.ca_bundle)).text)
    assert {"newNonce", "newAccount", "newOrder"} <= set(data)
