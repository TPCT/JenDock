"""The host helper against a real Apache (``httpd:2.4``) running ``configtest`` in a container.

Run with ``make test-docker``. The container is started per configtest with ``--rm`` and the
label ``shiftcodes.test=1``; an ``httpd:2.4`` image that was not present is pulled and removed
again. Graceful reload is simulated by the wrapper script (there is no running server to
reload); everything that decides whether a change is kept is real: the file is written by the
helper, Apache itself parses it, and a failure must restore the previous file.
"""

from __future__ import annotations

import shutil
import stat
import subprocess
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from shiftcodes.hosthelper.apache import ApacheControl
from shiftcodes.hosthelper.config import HelperConfig, PortRange
from shiftcodes.hosthelper.errors import HelperError
from shiftcodes.hosthelper.service import HelperService
from tests.docker_support import TEST_LABEL, docker_available, image_present, remove_image
from tests.ingress_support import make_certificate

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not docker_available(), reason="no Docker daemon available"),
]
IMAGE = "httpd:2.4"
# Besides the shared test label every container carries a label unique to this module, so the
# leftover check is not confused by resources other test runs create in parallel.
OWN_LABEL = f"shiftcodes.test.owner=apache-{uuid.uuid4().hex[:12]}"
HOST = "app.example.com"
MODULES = [
    "mpm_event",
    "authz_core",
    "unixd",
    "alias",
    "proxy",
    "proxy_http",
    "proxy_wstunnel",
    "rewrite",
    "headers",
    "ssl",
    "socache_shmcb",
]

CONFIG = """ServerRoot "/usr/local/apache2"
ServerName localhost
Listen 80
Listen 443
PidFile /tmp/httpd.pid
User daemon
Group daemon
{modules}
IncludeOptional {vhosts}/sc-*.conf
"""

WRAPPER = """#!/bin/sh
case "$1" in
  configtest)
    exec {docker} run --rm --label {label} --label {own} -v "{root}:{root}:ro" {image} \\
      httpd -t -f "{root}/httpd.conf" ;;
  graceful)
    if [ -e "{root}/fail-reload" ]; then echo "AH00016: reload refused" >&2; exit 1; fi
    echo reloaded >> "{root}/reloads.log" ;;
esac
"""


@pytest.fixture(scope="module", autouse=True)
def apache_image() -> Iterator[None]:
    pulled = not image_present(IMAGE)
    if pulled:
        result = subprocess.run(  # noqa: S603
            [shutil.which("docker") or "docker", "pull", "--quiet", IMAGE],
            capture_output=True,
            check=False,
            timeout=300,
        )
        assert result.returncode == 0, result.stderr
    yield
    if pulled:
        remove_image(IMAGE)
    leftovers = subprocess.run(  # noqa: S603
        [
            shutil.which("docker") or "docker",
            "ps",
            "--all",
            "--quiet",
            "--filter",
            f"label={OWN_LABEL}",
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout.split()
    assert leftovers == [], "test containers remain"


class Site:
    """A temp tree with an httpd.conf, a wrapper apachectl and a helper service."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.vhosts = root / "vhosts"
        self.vhosts.mkdir()
        self.write_httpd(MODULES)
        wrapper = root / "apachectl.sh"
        wrapper.write_text(
            WRAPPER.format(
                docker=shutil.which("docker"),
                label=TEST_LABEL,
                own=OWN_LABEL,
                root=root,
                image=IMAGE,
            )
        )
        wrapper.chmod(stat.S_IRWXU)
        self.config = HelperConfig(
            socket_path=root / "h.sock",
            socket_mode=0o660,
            socket_group=None,
            allowed_uids=frozenset({0}),
            vhost_dir=self.vhosts,
            cert_dir=root / "certs",
            webroot_dir=root / "webroot",
            apachectl=(str(wrapper),),
            allowed_hostname_suffixes=("example.com",),
            allowed_upstream_ports=(PortRange(20000, 29999),),
            command_timeout_seconds=120.0,
        )
        self.service = HelperService(self.config, ApacheControl(self.config.apachectl, 120.0))

    def write_httpd(self, modules: list[str]) -> None:
        lines = "\n".join(f"LoadModule {m}_module modules/mod_{m}.so" for m in modules)
        (self.root / "httpd.conf").write_text(CONFIG.format(modules=lines, vhosts=self.vhosts))

    def put(self, port: int = 20001, *, tls: bool = False) -> dict[str, object]:
        return self.service.execute(
            "vhost.put",
            {"hostname": HOST, "upstream_port": port, "tls": tls, "websocket": True},
        )

    @property
    def reloads(self) -> int:
        log = self.root / "reloads.log"
        return len(log.read_text().splitlines()) if log.exists() else 0


@pytest.fixture
def site(tmp_path_factory: pytest.TempPathFactory) -> Site:
    return Site(tmp_path_factory.mktemp("apache"))


def test_real_apache_accepts_the_generated_http_vhost(site: Site) -> None:
    assert site.put() == {"changed": True}
    assert (site.vhosts / f"sc-{HOST}.conf").is_file()
    assert site.reloads == 1
    assert site.service.execute("apache.configtest", {})["ok"] is True


def test_real_apache_accepts_the_generated_tls_vhost(site: Site) -> None:
    now = datetime.now(UTC)
    chain, key = make_certificate(HOST, now - timedelta(days=1), now + timedelta(days=60))
    site.service.execute(
        "cert.install", {"hostname": HOST, "fullchain_pem": chain, "private_key_pem": key}
    )
    assert site.put(tls=True) == {"changed": True}
    result = site.service.execute("apache.configtest", {})
    assert result["ok"] is True, result["output"]
    # Renewal: a new certificate is installed and the (now TLS) vhost is re-tested and reloaded.
    before = site.reloads
    new_chain, new_key = make_certificate(HOST, now - timedelta(days=1), now + timedelta(days=89))
    site.service.execute(
        "cert.install", {"hostname": HOST, "fullchain_pem": new_chain, "private_key_pem": new_key}
    )
    assert site.reloads == before + 1
    assert (site.config.cert_dir / HOST / "fullchain.pem").read_text() == new_chain


def test_a_config_that_apache_rejects_is_reverted_for_a_new_vhost(site: Site) -> None:
    site.write_httpd([m for m in MODULES if m != "headers"])  # the template needs mod_headers
    with pytest.raises(HelperError) as raised:
        site.put()
    assert raised.value.code == "configtest_failed"
    assert "Invalid command" in raised.value.message  # Apache's own diagnostics are passed on
    assert list(site.vhosts.iterdir()) == []
    assert site.reloads == 0


def test_a_config_that_apache_rejects_restores_the_previous_vhost(site: Site) -> None:
    site.put(port=20001)
    path = site.vhosts / f"sc-{HOST}.conf"
    before = path.read_text()
    site.write_httpd([m for m in MODULES if m != "headers"])
    with pytest.raises(HelperError) as raised:
        site.put(port=20002)
    assert raised.value.code == "configtest_failed"
    assert path.read_text() == before
    assert "20002" not in path.read_text()
    assert [p.name for p in site.vhosts.iterdir()] == [path.name]
    # With the module available again the restored file is valid, i.e. the revert is sound.
    site.write_httpd(MODULES)
    assert site.service.execute("apache.configtest", {})["ok"] is True
    assert site.reloads == 1  # only the first, successful change reloaded


def test_a_failed_reload_after_a_passing_configtest_is_reverted(site: Site) -> None:
    site.put(port=20001)
    path = site.vhosts / f"sc-{HOST}.conf"
    before = path.read_text()
    (site.root / "fail-reload").write_text("1")
    with pytest.raises(HelperError) as raised:
        site.put(port=20002)
    assert raised.value.code == "reload_failed"
    assert path.read_text() == before


def test_a_failed_cert_install_restores_the_previous_certificate(site: Site) -> None:
    now = datetime.now(UTC)
    chain, key = make_certificate(HOST, now - timedelta(days=1), now + timedelta(days=60))
    site.service.execute(
        "cert.install", {"hostname": HOST, "fullchain_pem": chain, "private_key_pem": key}
    )
    site.put(tls=True)
    site.write_httpd([m for m in MODULES if m != "ssl"])  # TLS directives now fail configtest
    new_chain, new_key = make_certificate(HOST, now - timedelta(days=1), now + timedelta(days=89))
    with pytest.raises(HelperError) as raised:
        site.service.execute(
            "cert.install",
            {"hostname": HOST, "fullchain_pem": new_chain, "private_key_pem": new_key},
        )
    assert raised.value.code == "configtest_failed"
    assert (site.config.cert_dir / HOST / "fullchain.pem").read_text() == chain
    assert (site.config.cert_dir / HOST / "privkey.pem").read_text() == key
