"""Shared helpers for API tests: clock, fast hasher, app builders and login."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

from argon2 import PasswordHasher as Argon2Hasher
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from backend.adapters.worker_client import GithubGateway, SecretsGateway, WorkerClient
from backend.api.app import create_asgi_app
from backend.api.container import AppContainer
from backend.config import Settings
from backend.domain.actor import Actor
from backend.repositories.database import UnitOfWork, create_session_factory
from backend.services.passwords import PasswordHasher
from backend.services.rate_limit import webhook_failure_limiter
from backend.services.secret_cipher import SecretCipher
from backend.services.secrets import SecretService
from backend.services.users import UserService
from backend.worker.handlers import build_handlers
from backend.worker.rpc import RpcServer

ORIGIN = "https://app.example.test"
DEV_ORIGIN = "http://localhost:8000"
USERNAME = "admin"
PASSWORD = "correct horse battery"


class FakeClock:
    """A settable clock."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        """Move time forward."""
        self.now += timedelta(**kwargs)


def fast_hasher() -> PasswordHasher:
    """An argon2id hasher with minimal cost so tests stay fast."""
    return PasswordHasher(Argon2Hasher(time_cost=1, memory_cost=8, parallelism=1))


def make_settings(
    tmp_path: Path,
    origin: str = ORIGIN,
    environment: str = "development",
    socket_path: Path | None = None,
) -> Settings:
    """Settings for a temp data dir; no key file exists in it."""
    return Settings.model_validate(
        {
            "data_dir": tmp_path / "data",
            "public_origin": origin,
            "environment": environment,
            "worker_socket_path": socket_path or tmp_path / "missing.sock",
        }
    )


def make_container(
    settings: Settings,
    engine: Engine,
    clock: FakeClock,
    *,
    secrets: SecretsGateway | None = None,
    github: GithubGateway | None = None,
) -> AppContainer:
    """Wire a container onto the test engine."""
    base = AppContainer.build(
        settings,
        secrets=secrets or WorkerClient(settings.worker_socket_path),
        github=github,
        clock=clock,
        hasher=fast_hasher(),
    )
    base.engine.dispose()
    return AppContainer(
        settings=settings,
        engine=engine,
        session_factory=create_session_factory(engine),
        clock=clock,
        hasher=base.hasher,
        limiter=base.limiter,
        webhook_limiter=webhook_failure_limiter(clock),
        secrets=base.secrets,
        cookies=base.cookies,
        github=github or base.github,
    )


def make_client(container: AppContainer) -> TestClient:
    """A TestClient whose base URL is the configured public origin."""
    return TestClient(
        create_asgi_app(container=container), base_url=container.settings.public_origin
    )


def create_user(container: AppContainer, username: str = USERNAME, password: str = PASSWORD) -> int:
    """Create a user through the real service and return its id."""
    service = UserService(container.uow(), container.hasher, container.clock)
    return service.create(username, password, Actor.system("test")).id


def login(client: TestClient, username: str = USERNAME, password: str = PASSWORD) -> str:
    """Log in with the correct Origin and return the CSRF token."""
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
        headers={"Origin": str(client.base_url).rstrip("/")},
    )
    assert response.status_code == 200, response.text
    token = response.json()["csrf_token"]
    assert isinstance(token, str)
    return token


def mutation_headers(client: TestClient, csrf: str) -> dict[str, str]:
    """Origin and CSRF headers for an unsafe cookie-authenticated request."""
    return {"Origin": str(client.base_url).rstrip("/"), "X-CSRF-Token": csrf}


@contextmanager
def running_worker(socket_dir: Path, engine: Engine, key_file: Path) -> Iterator[Path]:
    """Run a real RPC server (real cipher, real services) on a short temp socket path."""
    socket_path = socket_dir / "w.sock"
    cipher = SecretCipher(key_file)
    session_factory = create_session_factory(engine)

    def service() -> SecretService:
        return SecretService(UnitOfWork(session_factory), cipher)

    server = RpcServer(socket_path, build_handlers(service))
    server.start()
    try:
        yield socket_path
    finally:
        server.stop()
