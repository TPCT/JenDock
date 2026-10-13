import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from backend.api.__main__ import server_options
from backend.api.app import create_app
from backend.api.container import AppContainer
from backend.api.deps import client_ip
from backend.config import Settings
from tests.api_support import ORIGIN, PASSWORD, USERNAME, make_settings

PROXY = "10.0.0.5"
LOGIN = "/api/auth/login"
BAD = "wrong password!"


def _app_with_ip_route(container: AppContainer) -> FastAPI:
    app = create_app(container=container)

    def whoami(request: Request) -> dict[str, str]:
        return {"ip": client_ip(request)}

    app.add_api_route("/ip", whoami, methods=["GET"])
    return app


def _client(container: AppContainer, trusted: str, peer: str = PROXY) -> TestClient:
    """The app behind uvicorn's real proxy-headers middleware, as ``python -m backend.api``."""
    app = _app_with_ip_route(container)
    wrapped = ProxyHeadersMiddleware(app, trusted_hosts=trusted)  # type: ignore[arg-type]
    return TestClient(wrapped, base_url=ORIGIN, client=(peer, 1234))  # type: ignore[arg-type]


def _ip(client: TestClient, forwarded: str) -> str:
    return str(client.get("/ip", headers={"X-Forwarded-For": forwarded}).json()["ip"])


def test_options_trust_nobody_by_default(tmp_path) -> None:  # type: ignore[no-untyped-def]
    options = server_options(make_settings(tmp_path))
    assert options.proxy_headers is False
    assert options.forwarded_allow_ips is None


def test_options_use_configured_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JENDOCK_TRUSTED_PROXIES", "172.18.0.1, 10.0.0.0/8")
    settings = Settings()
    assert settings.trusted_proxies == ["172.18.0.1", "10.0.0.0/8"]
    options = server_options(settings)
    assert options.proxy_headers is True
    assert options.forwarded_allow_ips == "172.18.0.1,10.0.0.0/8"


@pytest.mark.parametrize("value", ["*", "not-an-ip", "10.0.0.1,bogus"])
def test_invalid_trusted_proxies_rejected(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("JENDOCK_TRUSTED_PROXIES", value)
    with pytest.raises(ValueError, match="trusted_proxies"):
        Settings()


def test_empty_env_means_no_trusted_proxies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JENDOCK_TRUSTED_PROXIES", "")
    assert Settings().trusted_proxies == []


def test_forwarded_for_ignored_when_peer_not_trusted(container: AppContainer) -> None:
    # What the entrypoint does with no trusted proxies: no proxy middleware at all.
    client = TestClient(_app_with_ip_route(container), client=(PROXY, 1))
    assert _ip(client, "203.0.113.9") == PROXY


def test_forwarded_for_ignored_when_peer_is_not_in_trusted_list(container: AppContainer) -> None:
    client = _client(container, trusted="192.0.2.1", peer=PROXY)
    assert _ip(client, "203.0.113.9") == PROXY


def test_forwarded_for_honoured_from_trusted_peer(container: AppContainer) -> None:
    client = _client(container, trusted=PROXY)
    assert _ip(client, "203.0.113.9") == "203.0.113.9"


def test_ip_limit_locks_out_only_that_ip_and_username_limit_survives_ip_rotation(
    container: AppContainer, admin: int
) -> None:
    client = _client(container, trusted=PROXY)
    headers = {"Origin": ORIGIN}

    def attempt(ip: str, username: str, password: str = BAD) -> int:
        body = {"username": username, "password": password}
        response = client.post(LOGIN, json=body, headers={**headers, "X-Forwarded-For": ip})
        return int(response.status_code)

    # one IP hammering unknown names is locked out after 20 failures ...
    for index in range(20):
        assert attempt("198.51.100.1", f"user{index}") == 401
    assert attempt("198.51.100.1", USERNAME, PASSWORD) == 429
    # ... but a different client IP can still log in as the admin
    assert attempt("198.51.100.2", USERNAME, PASSWORD) == 200
    # rotating IPs does not bypass the per-username limit
    for index in range(5):
        assert attempt(f"203.0.113.{index}", "victim") == 401
    assert attempt("203.0.113.200", "victim") == 429
