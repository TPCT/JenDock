import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from shiftcodes.api.container import AppContainer
from shiftcodes.domain.enums import EnvironmentName
from shiftcodes.repositories.database import (
    UnitOfWork,
    create_db_engine,
    create_session_factory,
)
from shiftcodes.repositories.models import Base
from shiftcodes.services.projects import ProjectService
from shiftcodes.services.secret_cipher import SecretCipher
from tests.api_support import FakeClock, create_user, make_client, make_container, make_settings
from tests.execution_support import Harness, make_harness

SLUG = "demo"
DEV = EnvironmentName.DEVELOPMENT
PROD = EnvironmentName.PRODUCTION


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    db = create_db_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(db)
    yield db
    db.dispose()


@pytest.fixture
def uow(engine: Engine) -> UnitOfWork:
    return UnitOfWork(create_session_factory(engine))


@pytest.fixture
def cipher(tmp_path: Path) -> SecretCipher:
    return SecretCipher(tmp_path / "keys" / "secret.key")


@pytest.fixture
def project(uow: UnitOfWork) -> str:
    ProjectService(uow).create(SLUG, "Demo", "octo/demo")
    return SLUG


@pytest.fixture
def socket_dir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="sc"))
    yield path
    shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def container(tmp_path: Path, engine: Engine, clock: FakeClock) -> AppContainer:
    """API container on the test database; the worker socket does not exist."""
    return make_container(make_settings(tmp_path), engine, clock)


@pytest.fixture
def client(container: AppContainer) -> TestClient:
    return make_client(container)


@pytest.fixture
def admin(container: AppContainer) -> int:
    """Create the default admin user; returns its id."""
    return create_user(container)


@pytest.fixture
def harness(tmp_path: Path, engine: Engine, cipher: SecretCipher, project: str) -> Harness:
    """Execution environment with fake Docker, source, deployer and probe."""
    return make_harness(tmp_path, engine, cipher, project)
