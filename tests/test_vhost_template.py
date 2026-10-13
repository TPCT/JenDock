from pathlib import Path

import pytest

from backend.hosthelper.vhost import VhostPaths, VhostSpec, render_vhost
from tests.ingress_support import FIXTURES

PATHS = VhostPaths(Path("/etc/jendock/certs"), Path("/var/lib/jendock/acme-webroot"))
GOLDEN = FIXTURES / "vhost"

CASES = {
    "http_websocket.conf": VhostSpec("app.example.com", 20001, tls=False, websocket=True),
    "https_websocket.conf": VhostSpec("app.example.com", 20001, tls=True, websocket=True),
    "https_no_websocket.conf": VhostSpec("api.example.com", 20002, tls=True, websocket=False),
    "https_wildcard.conf": VhostSpec("*.example.com", 20003, tls=True, websocket=True),
}


@pytest.mark.parametrize("name", sorted(CASES))
def test_rendering_matches_the_golden_file(name: str) -> None:
    assert render_vhost(CASES[name], PATHS) == (GOLDEN / name).read_text()


def test_every_golden_file_is_covered() -> None:
    assert {p.name for p in GOLDEN.glob("*.conf")} == set(CASES)


def test_the_tls_vhost_redirects_http_but_keeps_the_acme_path_open() -> None:
    text = render_vhost(CASES["https_websocket.conf"], PATHS)
    assert "RewriteRule ^/?(.*)$ https://app.example.com/$1 [R=301,L]" in text
    assert r"RewriteCond %{REQUEST_URI} !^/\.well-known/acme-challenge/" in text
    assert "Alias /.well-known/acme-challenge/" in text
    assert text.count("<VirtualHost") == 2


def test_the_plain_vhost_proxies_directly_and_excludes_the_challenge_path() -> None:
    text = render_vhost(CASES["http_websocket.conf"], PATHS)
    assert "ProxyPass /.well-known/acme-challenge/ !" in text
    assert text.index("ProxyPass /.well-known") < text.index("ProxyPass / http")
    assert "443" not in text and "SSLEngine" not in text
    assert 'RequestHeader set X-Forwarded-Proto "http"' in text


def test_security_headers_never_override_what_the_application_sets() -> None:
    text = render_vhost(CASES["https_websocket.conf"], PATHS)
    for header in ("Strict-Transport-Security", "X-Content-Type-Options", "X-Frame-Options"):
        assert f"Header always setifempty {header}" in text


def test_websocket_upgrade_is_proxied_to_the_loopback_port_only() -> None:
    text = render_vhost(CASES["https_websocket.conf"], PATHS)
    assert "RewriteCond %{HTTP:Upgrade} =websocket [NC]" in text
    assert "ws://127.0.0.1:20001/$1 [P,L]" in text
    assert "ws://" not in render_vhost(CASES["https_no_websocket.conf"], PATHS)


def test_the_only_upstream_is_the_loopback_interface() -> None:
    for spec in CASES.values():
        text = render_vhost(spec, PATHS)
        upstreams = {line.split()[2] for line in text.splitlines() if "ProxyPass / " in line}
        assert upstreams == {f"http://127.0.0.1:{spec.upstream_port}/"}


def test_the_wildcard_redirect_never_uses_an_unchecked_host_header() -> None:
    text = render_vhost(CASES["https_wildcard.conf"], PATHS)
    assert "%{HTTP_HOST}" in text and "%1.example.com" in text
    assert "ServerAlias *.example.com" in text
    assert "_wildcard_.example.com/fullchain.pem" in text
