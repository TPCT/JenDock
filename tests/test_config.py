import os
from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("JENDOCK_"):
            monkeypatch.delenv(name)


def test_defaults() -> None:
    settings = Settings()
    assert settings.environment == "development"
    assert settings.docker_host == "unix:///var/run/docker.sock"
    assert settings.database_url == f"sqlite:///{settings.data_dir / 'jendock.db'}"
    assert settings.secret_key_file == settings.data_dir / "secret.key"
    assert settings.log_level == "INFO"


def test_data_dir_drives_derived_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JENDOCK_DATA_DIR", "/srv/jendock")
    settings = Settings()
    assert settings.database_url == "sqlite:////srv/jendock/jendock.db"
    assert settings.secret_key_file == Path("/srv/jendock/secret.key")


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JENDOCK_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("JENDOCK_DATABASE_URL", "postgresql://x/y")
    settings = Settings()
    assert settings.log_level == "DEBUG"
    assert settings.database_url == "postgresql://x/y"


def test_production_rejects_http_origin() -> None:
    with pytest.raises(ValidationError):
        Settings(environment="production", public_origin="http://example.com")


def test_development_allows_http_origin() -> None:
    settings = Settings(public_origin="http://localhost:8000")
    assert settings.public_origin == "http://localhost:8000"


@pytest.mark.parametrize(
    "origin", ["https://example.com/path", "https://u:p@example.com", "https://", "ftp://x"]
)
def test_rejects_malformed_origin(origin: str) -> None:
    with pytest.raises(ValidationError):
        Settings(public_origin=origin)


def test_invalid_environment_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(environment="staging")


def test_get_settings_is_cached() -> None:
    get_settings.cache_clear()
    assert get_settings() is get_settings()
    get_settings.cache_clear()


def test_execution_defaults() -> None:
    settings = Settings()
    assert settings.docker_binary == "docker"
    assert settings.workspace_max_bytes == 2 * 1024**3
    assert settings.step_log_max_bytes == 5 * 1024**2
    assert settings.parallel_workers == 2
    assert settings.max_concurrent_runs == 1
    assert (settings.container_memory, settings.container_cpus, settings.build_memory) == (
        "2g",
        "2",
        "2g",
    )
    assert settings.workspace_host_dir is None


def test_execution_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JENDOCK_DOCKER_BINARY", "/opt/docker/bin/docker")
    monkeypatch.setenv("JENDOCK_MAX_CONCURRENT_RUNS", "3")
    monkeypatch.setenv("JENDOCK_CONTAINER_MEMORY", "512m")
    monkeypatch.setenv("JENDOCK_CONTAINER_CPUS", "1.5")
    settings = Settings()
    assert settings.docker_binary == "/opt/docker/bin/docker"
    assert settings.max_concurrent_runs == 3
    assert (settings.container_memory, settings.container_cpus) == ("512m", "1.5")


@pytest.mark.parametrize(
    "values",
    [
        {"container_memory": "lots"},
        {"build_memory": "0"},
        {"container_cpus": "-1"},
        {"container_cpus": "many"},
        {"max_concurrent_runs": 0},
        {"parallel_workers": 0},
        {"workspace_max_bytes": 0},
    ],
)
def test_invalid_execution_settings_are_rejected(values: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate(values)
