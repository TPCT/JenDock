"""Engine and session factory creation, and the unit of work."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from sqlite3 import Connection as SQLiteConnection
from types import TracebackType
from typing import Self

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import ConnectionPoolEntry

from shiftcodes.repositories.audit import AuditRepository
from shiftcodes.repositories.environments import EnvironmentRepository
from shiftcodes.repositories.pipelines import PipelineRepository
from shiftcodes.repositories.projects import ProjectRepository
from shiftcodes.repositories.runs import RunRepository
from shiftcodes.repositories.secrets import SecretRepository
from shiftcodes.repositories.sessions import SessionRepository
from shiftcodes.repositories.tokens import ApiTokenRepository
from shiftcodes.repositories.users import UserRepository

SessionFactory = Callable[[], Session]
SQLITE_BUSY_TIMEOUT_MS = 5000


def create_db_engine(url: str) -> Engine:
    """Create an engine; SQLite gets foreign keys, WAL and a busy timeout."""
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return create_engine(url)
    if parsed.database and parsed.database != ":memory:":
        _prepare_sqlite_file(Path(parsed.database))
    engine = create_engine(url)
    event.listen(engine, "connect", _configure_sqlite)
    return engine


def _prepare_sqlite_file(path: Path) -> None:
    """Create the database file group-writable (0660 before umask).

    SQLite itself creates files as 0644, which would lock out a second process in the same
    group; its -wal and -shm files copy the mode of the main file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o660))
    except FileExistsError:
        return


def _configure_sqlite(dbapi_connection: SQLiteConnection, _record: ConnectionPoolEntry) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    cursor.close()


def create_session_factory(engine: Engine) -> SessionFactory:
    """Return a factory of sessions that keep loaded attributes after commit."""
    return sessionmaker(engine, expire_on_commit=False)


class UnitOfWork:
    """Transaction boundary: commits on success, rolls back on any exception.

    Re-usable (one transaction per ``with`` block) but not re-entrant.
    """

    def __init__(self, session_factory: SessionFactory) -> None:
        self._session_factory = session_factory
        self._session: Session | None = None

    def __enter__(self) -> Self:
        if self._session is not None:
            raise RuntimeError("UnitOfWork is already active")
        self._session = self._session_factory()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        session = self.session
        try:
            if exc_type is None:
                session.commit()
            else:
                session.rollback()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()
            self._session = None

    @property
    def session(self) -> Session:
        """The active session; raises if used outside ``with``."""
        if self._session is None:
            raise RuntimeError("UnitOfWork is not active")
        return self._session

    @property
    def users(self) -> UserRepository:
        """User repository bound to the active session."""
        return UserRepository(self.session)

    @property
    def sessions(self) -> SessionRepository:
        """Login session repository bound to the active session."""
        return SessionRepository(self.session)

    @property
    def tokens(self) -> ApiTokenRepository:
        """API token repository bound to the active session."""
        return ApiTokenRepository(self.session)

    @property
    def projects(self) -> ProjectRepository:
        """Project repository bound to the active session."""
        return ProjectRepository(self.session)

    @property
    def environments(self) -> EnvironmentRepository:
        """Environment repository bound to the active session."""
        return EnvironmentRepository(self.session)

    @property
    def pipelines(self) -> PipelineRepository:
        """Pipeline definition repository bound to the active session."""
        return PipelineRepository(self.session)

    @property
    def runs(self) -> RunRepository:
        """Run repository bound to the active session."""
        return RunRepository(self.session)

    @property
    def secrets(self) -> SecretRepository:
        """Secret repository bound to the active session."""
        return SecretRepository(self.session)

    @property
    def audit(self) -> AuditRepository:
        """Audit repository bound to the active session."""
        return AuditRepository(self.session)
