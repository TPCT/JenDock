import signal
from pathlib import Path

from backend.adapters.docker_cli import DockerCliRuntime
from backend.adapters.http_probe import UrllibProbe
from backend.adapters.worker_client import WorkerClient
from backend.config import Settings
from backend.logging import SecretRedactor
from backend.repositories.database import create_db_engine, create_session_factory
from backend.repositories.models import Base
from backend.worker.__main__ import ShutdownHandler, build_rpc_server, build_scheduler


def test_handler_sets_flag() -> None:
    handler = ShutdownHandler()
    assert not handler.requested
    handler.handle(signal.SIGTERM, None)
    assert handler.requested


def test_wait_returns_immediately_after_shutdown() -> None:
    handler = ShutdownHandler()
    handler.handle(signal.SIGTERM, None)
    assert handler.wait(10) is True


def test_build_rpc_server_owns_the_key_file(tmp_path: Path) -> None:
    settings = Settings.model_validate(
        {
            "data_dir": tmp_path / "d",
            "worker_socket_path": tmp_path / "w.sock",
            "secret_key_file": tmp_path / "keys" / "secret.key",
        }
    )
    engine = create_db_engine(settings.database_url)
    Base.metadata.create_all(engine)
    server = build_rpc_server(settings, create_session_factory(engine), SecretRedactor())
    assert settings.secret_key_file.exists()
    server.start()
    try:
        WorkerClient(settings.worker_socket_path).ping()
    finally:
        server.stop()
        engine.dispose()
    assert not settings.worker_socket_path.exists()


def test_scheduler_wiring_uses_settings(tmp_path: Path) -> None:
    settings = Settings.model_validate(
        {
            "data_dir": tmp_path / "d",
            "docker_binary": "/opt/docker/bin/docker",
            "max_concurrent_runs": 3,
            "workspace_max_bytes": 12345,
        }
    )
    engine = create_db_engine(settings.database_url)
    scheduler = build_scheduler(settings, create_session_factory(engine), SecretRedactor())
    env = scheduler.env
    assert isinstance(env.runtime, DockerCliRuntime)
    assert isinstance(env.probe, UrllibProbe)
    assert env.config.max_concurrent_runs == 3
    assert env.logs.path(1, 2) == tmp_path / "d" / "logs" / "1" / "2.log"
    assert env.workspaces.path(5) == tmp_path / "d" / "workspaces" / "5"
    engine.dispose()
