"""``vhost.discover``: the read-only listing of existing Apache sites."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from backend.hosthelper.config import parse_config
from backend.hosthelper.discovery import parse_vhosts
from backend.hosthelper.errors import InvalidRequestError
from backend.hosthelper.server import handle_line
from tests.host_helper_support import make_service

HAND_MADE = """\
# managed by hand
<VirtualHost *:80>
    ServerName app.example.com
    ServerAlias www.app.example.com
    Redirect permanent / https://app.example.com/
</VirtualHost>
<VirtualHost *:443>
    ServerName App.Example.com:443
    ServerAlias www.app.example.com "quoted.example.com"
    SSLEngine on
    SSLCertificateKeyFile /etc/ssl/private/secret.key
    ProxyPass /static !
    ProxyPass /api http://127.0.0.1:9000/api
    ProxyPass / http://127.0.0.1:20001/
    ProxyPassReverse / http://127.0.0.1:20001/
</VirtualHost>
"""


def test_parser_reads_names_upstream_and_tls() -> None:
    blocks = parse_vhosts(HAND_MADE, "app.conf", managed=False)
    assert len(blocks) == 2
    redirect, proxy = (b.as_payload() for b in blocks)
    assert redirect["upstream_port"] is None and redirect["ssl"] is False
    assert proxy["server_names"] == ["app.example.com", "www.app.example.com", "quoted.example.com"]
    assert (proxy["upstream_host"], proxy["upstream_port"], proxy["ssl"]) == (
        "127.0.0.1",
        20001,  # the "/" proxy wins over "/api"
        True,
    )
    assert "secret" not in str(proxy)  # nothing but names, upstream and TLS leaves the host


def test_parser_handles_continuations_comments_rewrite_proxies_and_junk() -> None:
    text = (
        "<VirtualHost 10.0.0.1:8443>\n"
        "  ServerName a.example.com\n"
        "  # ProxyPass / http://127.0.0.1:1111/\n"
        "  RewriteRule ^/(.*)$ http://localhost:3000/$1 [P,L]\n"
        "  ServerAlias \\\n    b.example.com *.wild.example.com bad_name !!\n"
        "</VirtualHost>\n"
        "<VirtualHost *:80>\n  DocumentRoot /var/www\n</VirtualHost>\n"  # no ServerName: skipped
        "ProxyPass / http://127.0.0.1:2222/\n"  # outside a block: ignored
    )
    [block] = parse_vhosts(text, "x.conf", managed=False)
    payload = block.as_payload()
    assert payload["server_names"] == ["a.example.com", "b.example.com", "*.wild.example.com"]
    assert (payload["upstream_host"], payload["upstream_port"]) == ("localhost", 3000)
    assert payload["ssl"] is False and payload["ports"] == [8443]


def test_discover_scans_the_managed_and_configured_directories_only(tmp_path: Path) -> None:
    service, _apache, config = make_service(tmp_path)
    other = tmp_path / "sites-enabled"
    other.mkdir()
    (other / "app.conf").write_text(HAND_MADE)
    (other / "notes.txt").write_text(HAND_MADE)  # not *.conf
    (other / "huge.conf").write_text("#" * (300 * 1024))  # over the size limit
    (config.vhost_dir / "jendock-x.example.com.conf").write_text(
        "# jendock-managed: host=x.example.com\n<VirtualHost *:80>\nServerName x.example.com\n"
        "ProxyPass / http://127.0.0.1:20002/\n</VirtualHost>\n"
    )
    service._config = replace(config, discovery_dirs=(other, tmp_path / "missing"))
    result = service.execute("vhost.discover", {})
    vhosts = result["vhosts"]
    assert isinstance(vhosts, list)
    by_file = {(v["file"], tuple(v["server_names"])): v for v in vhosts}
    assert ("jendock-x.example.com.conf", ("x.example.com",)) in by_file
    assert by_file[("jendock-x.example.com.conf", ("x.example.com",))]["managed"] is True
    assert any(f == "app.conf" for f, _n in by_file)
    assert result["files_scanned"] == 2 and result["truncated"] is False


def test_discover_is_read_only_and_takes_no_parameters(tmp_path: Path) -> None:
    service, apache, config = make_service(tmp_path)
    (config.vhost_dir / "a.conf").write_text(HAND_MADE)
    before = {p.name: p.read_bytes() for p in config.vhost_dir.iterdir()}
    service.execute("vhost.discover", {})
    assert {p.name: p.read_bytes() for p in config.vhost_dir.iterdir()} == before
    assert apache.events == []  # no configtest, no reload, nothing executed
    with pytest.raises(InvalidRequestError):
        service.execute("vhost.discover", {"path": "/etc/shadow"})


def test_discover_over_the_wire_still_needs_a_valid_request(tmp_path: Path) -> None:
    service, _apache, _config = make_service(tmp_path)
    ok = handle_line(service, b'{"id": "1", "op": "vhost.discover", "params": {}}')
    assert ok["ok"] is True
    bad = handle_line(service, b'{"id": "2", "op": "vhost.discover", "params": {"dir": "/"}}')
    assert bad["ok"] is False


def test_config_rejects_relative_discovery_dirs() -> None:
    with pytest.raises(Exception, match="absolute"):
        parse_config({**_raw_example(), "discovery_dirs": ["sites-enabled"]})


def _raw_example() -> dict[str, object]:
    return {
        "socket_path": "/run/x.sock",
        "allowed_uids": [1],
        "vhost_dir": "/etc/apache2/jendock",
        "cert_dir": "/etc/jendock/certs",
        "webroot_dir": "/var/lib/acme",
        "apachectl": ["/usr/sbin/apachectl"],
        "allowed_hostname_suffixes": ["example.com"],
        "allowed_upstream_ports": [[20000, 29999]],
    }
