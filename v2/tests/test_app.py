from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from backend.api.app import create_app, create_asgi_app, wrap_security_headers
from backend.api.container import AppContainer
from tests.api_support import FakeClock, make_container, make_settings

EXPECTED = {
    "content-security-policy": "default-src 'self'; frame-ancestors 'none'; base-uri 'none'",
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
    "x-frame-options": "DENY",
}


def test_healthz(client: TestClient) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("get", "/healthz", 200),
        ("get", "/api/auth/session", 401),
        ("get", "/nope", 404),
        ("post", "/api/auth/login", 422),
        ("get", "/api/projects/demo/environments/development/secrets", 401),
    ],
)
def test_security_headers_on_every_response(
    client: TestClient, method: str, path: str, status: int
) -> None:
    response = client.request(method, path, headers={"Origin": "https://app.example.test"})
    assert response.status_code == status
    for name, value in EXPECTED.items():
        assert response.headers[name] == value
    assert "strict-transport-security" not in response.headers


def test_hsts_in_production(tmp_path: Path, engine: Engine, clock: FakeClock) -> None:
    settings = make_settings(tmp_path, environment="production")
    client = TestClient(create_asgi_app(container=make_container(settings, engine, clock)))
    response = client.get("/healthz")
    assert "max-age=" in response.headers["strict-transport-security"]
    assert client.get("/openapi.json").status_code == 404


def test_openapi_only_in_development(client: TestClient) -> None:
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 404


def test_app_builds_without_any_key_file(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    assert not settings.secret_key_file.exists()
    container = AppContainer.build(settings)
    app = create_asgi_app(container=container)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
    assert not settings.secret_key_file.exists()
    assert not list(tmp_path.rglob("*.key"))


def test_create_app_from_settings_only(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    app = create_asgi_app(settings)
    assert TestClient(app).get("/healthz").status_code == 200


def test_errors_use_one_envelope(client: TestClient) -> None:
    body = client.get("/api/auth/session").json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}


def test_unhandled_500_still_carries_security_headers(container: AppContainer) -> None:
    app = create_app(container=container)

    def boom() -> None:
        raise RuntimeError("kaboom")

    app.add_api_route("/boom", boom)
    wrapped = wrap_security_headers(app, container.settings)
    client = TestClient(wrapped, raise_server_exceptions=False)
    response = client.get("/boom")
    assert response.status_code == 500
    for name, value in EXPECTED.items():
        assert response.headers[name] == value
    assert "kaboom" not in response.text
