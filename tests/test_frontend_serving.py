"""Serving the built UI: static files, client-route fallback, caching, API errors untouched."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from backend.api.container import AppContainer
from backend.api.middleware import CONTENT_SECURITY_POLICY
from backend.domain.errors import ConfigurationError
from tests.api_support import FakeClock, login, make_client, make_container, make_settings

INDEX = "<!doctype html><title>app</title><script type=module src=/assets/app-abc123.js></script>"


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX)
    (root / "assets" / "app-abc123.js").write_text("console.log(1)")
    (root / "assets" / "app-abc123.css").write_text("body{}")
    (root / "favicon.svg").write_text("<svg/>")
    (tmp_path / "secret.txt").write_text("outside")
    return root


@pytest.fixture
def container(tmp_path: Path, engine: Engine, clock: FakeClock, dist: Path) -> AppContainer:
    settings = make_settings(tmp_path).model_copy(update={"frontend_dist": dist})
    return make_container(settings, engine, clock)


def test_root_serves_index_without_caching(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.text == INDEX
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "path", ["/login", "/projects/shop-api", "/projects/shop/environments/development/pipeline"]
)
def test_client_routes_fall_back_to_index(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert response.text == INDEX
    assert response.headers["cache-control"] == "no-store"


def test_hashed_assets_are_cached_for_a_year(client: TestClient) -> None:
    response = client.get("/assets/app-abc123.js")
    assert response.status_code == 200
    assert response.text == "console.log(1)"
    assert response.headers["cache-control"] == "public, max-age=31536000, immutable"
    assert "javascript" in response.headers["content-type"]
    css = client.get("/assets/app-abc123.css")
    assert css.headers["content-type"].startswith("text/css")


def test_other_static_files_keep_no_store(client: TestClient) -> None:
    response = client.get("/favicon.svg")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


def test_missing_files_are_404_not_index(client: TestClient) -> None:
    for path in ("/assets/missing.js", "/assets/nope", "/robots.txt", "/x/y/z.css", "/missing.map"):
        response = client.get(path)
        assert response.status_code == 404, path
        assert response.json()["error"]["code"] == "not_found"


def test_unknown_api_paths_keep_the_json_error_envelope(client: TestClient) -> None:
    for method in ("get", "post", "put", "delete"):
        response = getattr(client, method)("/api/unknown")
        assert response.status_code == 404, method
        assert response.headers["content-type"] == "application/json"
        assert response.json()["error"]["code"] == "not_found"
    assert client.get("/api").status_code == 404
    assert client.get("/api/").json()["error"]["code"] == "not_found"
    assert client.get("/api/projects/demo/unknown").json()["error"]["code"] == "not_found"


def test_api_and_health_routes_are_not_shadowed(client: TestClient, admin: int) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/api/auth/session").status_code == 401
    csrf = login(client)
    assert client.get("/api/auth/session").json()["csrf_token"] == csrf
    assert client.get("/api/pipelines/catalog").status_code == 200
    assert client.get("/healthz/extra").status_code == 404


def test_non_get_on_a_page_path_is_405_with_the_envelope(client: TestClient) -> None:
    response = client.post("/projects/x")
    assert response.status_code == 405
    assert response.json()["error"]["code"] == "http_error"


def test_security_headers_are_unchanged_on_every_kind_of_response(client: TestClient) -> None:
    for path in ("/", "/projects/x", "/assets/app-abc123.js", "/api/unknown", "/healthz"):
        headers = client.get(path).headers
        assert headers["content-security-policy"] == CONTENT_SECURITY_POLICY, path
        assert headers["x-content-type-options"] == "nosniff", path
        assert headers["x-frame-options"] == "DENY", path
        assert headers["referrer-policy"] == "same-origin", path


def test_api_responses_still_default_to_no_store(client: TestClient, admin: int) -> None:
    login(client)
    assert client.get("/api/auth/session").headers["cache-control"] == "no-store"
    assert client.get("/api/unknown").headers["cache-control"] == "no-store"


def test_path_traversal_cannot_leave_the_directory(client: TestClient) -> None:
    for path in ("/../secret.txt", "/assets/../../secret.txt", "/%2e%2e/secret.txt"):
        body = client.get(path).text
        assert "outside" not in body, path


def test_conditional_requests_work_for_assets(client: TestClient) -> None:
    first = client.get("/assets/app-abc123.js")
    again = client.get("/assets/app-abc123.js", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304


def test_without_a_frontend_directory_there_is_no_ui(
    tmp_path: Path, engine: Engine, clock: FakeClock
) -> None:
    bare = make_client(make_container(make_settings(tmp_path), engine, clock))
    assert bare.get("/").status_code == 404
    assert bare.get("/").json()["error"]["code"] == "not_found"


def test_a_directory_without_index_is_a_configuration_error(
    tmp_path: Path, engine: Engine, clock: FakeClock
) -> None:
    settings = make_settings(tmp_path).model_copy(update={"frontend_dist": tmp_path / "empty"})
    with pytest.raises(ConfigurationError):
        make_client(make_container(settings, engine, clock))
