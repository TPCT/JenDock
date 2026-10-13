import os
import stat
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from backend.repositories.database import UnitOfWork, create_db_engine
from backend.repositories.models import Project, User


def _project(slug: str = "p") -> Project:
    return Project(slug=slug, name="P", repository="o/r", created_at=datetime.now(UTC))


def test_commits_on_success(uow: UnitOfWork) -> None:
    with uow:
        uow.projects.add(_project())
    with uow:
        assert uow.projects.get_by_slug("p") is not None


def test_rolls_back_on_exception(uow: UnitOfWork) -> None:
    with pytest.raises(RuntimeError), uow:
        uow.projects.add(_project())
        raise RuntimeError("boom")
    with uow:
        assert uow.projects.get_by_slug("p") is None


def test_not_active_outside_with(uow: UnitOfWork) -> None:
    with pytest.raises(RuntimeError):
        _ = uow.projects


def test_not_reentrant(uow: UnitOfWork) -> None:
    with uow, pytest.raises(RuntimeError), uow:
        pass


def test_reusable_after_failure(uow: UnitOfWork) -> None:
    with pytest.raises(ValueError), uow:
        raise ValueError
    with uow:
        uow.projects.add(_project())


def test_sqlite_foreign_keys_and_wal(engine: Engine, uow: UnitOfWork) -> None:
    with engine.connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert connection.execute(text("PRAGMA journal_mode")).scalar() == "wal"
    with pytest.raises(IntegrityError), uow:
        uow.session.execute(
            text(
                "INSERT INTO environments (project_id, name, branch, auto_deploy, "
                "compose_files, services, created_at, updated_at) "
                "VALUES (999, 'development', 'b', 0, '[]', '[]', '2026-01-01', '2026-01-01')"
            )
        )


def test_timestamps_roundtrip_as_utc(uow: UnitOfWork) -> None:
    local = datetime(2026, 1, 1, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    with uow:
        uow.users.add(User(username="u", password_hash="h", created_at=local))
    with uow:
        user = uow.users.get_by_username("u")
        assert user is not None
        assert user.created_at == local
        assert user.created_at.utcoffset() == timedelta(0)


def test_naive_datetime_rejected(uow: UnitOfWork) -> None:
    with pytest.raises(Exception, match="naive"), uow:
        uow.users.add(User(username="u", password_hash="h", created_at=datetime(2026, 1, 1)))


def test_sqlite_file_is_created_group_writable(tmp_path: Path) -> None:
    previous = os.umask(0o007)
    try:
        engine = create_db_engine(f"sqlite:///{tmp_path / 'sub' / 'x.db'}")
        with engine.connect():
            pass
        engine.dispose()
    finally:
        os.umask(previous)
    assert stat.S_IMODE((tmp_path / "sub" / "x.db").stat().st_mode) == 0o660
