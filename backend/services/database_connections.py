"""Database connection configuration use cases (API side).

The password never lives in the connection row: it is written to the environment's encrypted
secret store through the worker (the API has no key) and the row only names that secret.
Secret writes happen outside the unit of work so the worker never waits on the API's open
database transaction.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from backend.adapters.worker_client import SecretsGateway
from backend.domain.actor import Actor
from backend.domain.audit import AuditEntry, AuditTarget
from backend.domain.clock import Clock, utcnow
from backend.domain.database import (
    DEFAULT_PORTS,
    MAX_CONNECTIONS_PER_ENVIRONMENT,
    ConnectionRef,
    DatabaseEngine,
    TlsMode,
    password_secret_name,
    validate_connection_name,
    validate_database_name,
    validate_host,
    validate_password_value,
    validate_port,
    validate_username,
)
from backend.domain.enums import EnvironmentName
from backend.domain.errors import ConflictError, NotFoundError, WorkerError
from backend.repositories.database import UnitOfWork
from backend.repositories.models import DatabaseConnection, Environment
from backend.services.audit import add_audit_event
from backend.services.lookup import require_environment

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class NewConnection:
    """Input of ``create``; ``port`` defaults to the engine's standard port."""

    name: str
    engine: DatabaseEngine
    host: str
    database: str
    username: str
    password: str
    port: int | None = None
    tls_mode: TlsMode = TlsMode.DISABLE
    network: str | None = None
    auto_detected: bool = False


@dataclass(frozen=True, slots=True)
class ConnectionChange:
    """Partial update; ``None`` means unchanged. A ``password`` rotates the stored secret."""

    engine: DatabaseEngine | None = None
    host: str | None = None
    port: int | None = None
    database: str | None = None
    username: str | None = None
    password: str | None = None
    tls_mode: TlsMode | None = None


class DatabaseConnectionService:
    """Create, read, change and delete the connections of an environment."""

    def __init__(
        self,
        uow: UnitOfWork,
        secrets: SecretsGateway,
        clock: Clock = utcnow,
    ) -> None:
        self._uow = uow
        self._secrets = secrets
        self._clock = clock

    def list(self, slug: str, env: EnvironmentName) -> Sequence[DatabaseConnection]:
        """Connections of the environment ordered by name."""
        with self._uow:
            environment = require_environment(self._uow, slug, env)
            return self._uow.database_connections.list_for_environment(environment.id)

    def get(self, slug: str, env: EnvironmentName, name: str) -> DatabaseConnection:
        """One connection or ``NotFoundError``."""
        with self._uow:
            environment = require_environment(self._uow, slug, env)
            return _require(self._uow, environment, name)

    def create(
        self, slug: str, env: EnvironmentName, new: NewConnection, actor: Actor
    ) -> DatabaseConnection:
        """Validate, store the password as a secret, then the connection row; audited."""
        name = validate_connection_name(new.name)
        fields = _validated_fields(new)
        password = validate_password_value(new.password)
        secret = password_secret_name(name)
        self._check_free(slug, env, name)
        self._secrets.set_secret(slug, env, secret, password, actor)
        try:
            return self._insert(ConnectionRef(slug, env, name), fields, secret, actor)
        except Exception:
            self._discard_secret(slug, env, secret, actor)
            raise

    def update(
        self, slug: str, env: EnvironmentName, name: str, change: ConnectionChange, actor: Actor
    ) -> DatabaseConnection:
        """Apply ``change``; a new password replaces the stored secret first."""
        validated = _validated_change(change)
        password = validate_password_value(change.password) if change.password is not None else None
        current = self.get(slug, env, name)
        if password is not None:
            self._secrets.set_secret(slug, env, current.password_secret, password, actor)
        with self._uow:
            environment = require_environment(self._uow, slug, env)
            connection = _require(self._uow, environment, name)
            _apply(connection, validated)
            connection.updated_at = self._clock()
            changed = sorted(_changed_fields(validated) | ({"password"} if password else set()))
            self._audit(actor, "database.connection.update", connection, {"changed": changed})
            return connection

    def delete(self, slug: str, env: EnvironmentName, name: str, actor: Actor) -> None:
        """Remove the connection and its password secret; audited."""
        with self._uow:
            environment = require_environment(self._uow, slug, env)
            connection = _require(self._uow, environment, name)
            secret = connection.password_secret
            self._audit(actor, "database.connection.delete", connection, {"name": connection.name})
            self._uow.database_backups.delete_for_connection(connection.id)
            self._uow.database_connections.delete(connection)
        self._discard_secret(slug, env, secret, actor)

    def _check_free(self, slug: str, env: EnvironmentName, name: str) -> None:
        with self._uow:
            environment = require_environment(self._uow, slug, env)
            if self._uow.database_connections.get(environment.id, name) is not None:
                raise ConflictError(f"database connection already exists: {name}")
            count = self._uow.database_connections.count_for_environment(environment.id)
            if count >= MAX_CONNECTIONS_PER_ENVIRONMENT:
                raise ConflictError(
                    f"at most {MAX_CONNECTIONS_PER_ENVIRONMENT} connections per environment"
                )

    def _insert(
        self, ref: ConnectionRef, fields: _Fields, secret: str, actor: Actor
    ) -> DatabaseConnection:
        name = ref.name
        now = self._clock()
        with self._uow:
            environment = require_environment(self._uow, ref.slug, ref.env)
            if self._uow.database_connections.get(environment.id, name) is not None:
                raise ConflictError(f"database connection already exists: {name}")
            connection = self._uow.database_connections.add(
                DatabaseConnection(
                    environment_id=environment.id,
                    name=name,
                    engine=fields.engine,
                    host=fields.host,
                    port=fields.port,
                    database=fields.database,
                    username=fields.username,
                    password_secret=secret,
                    tls_mode=fields.tls_mode,
                    network=fields.network,
                    auto_detected=fields.auto_detected,
                    created_at=now,
                    updated_at=now,
                )
            )
            details: dict[str, object] = {
                "name": name,
                "engine": fields.engine.value,
                "host": fields.host,
                "port": fields.port,
                "database": fields.database,
                "username": fields.username,
                "tls_mode": fields.tls_mode.value,
            }
            self._audit(actor, "database.connection.create", connection, details)
            return connection

    def _discard_secret(self, slug: str, env: EnvironmentName, secret: str, actor: Actor) -> None:
        try:
            self._secrets.delete_secret(slug, env, secret, actor)
        except NotFoundError:
            return
        except WorkerError:
            logger.warning("password secret could not be removed", extra={"secret_name": secret})

    def _audit(
        self, actor: Actor, action: str, connection: DatabaseConnection, details: dict[str, object]
    ) -> None:
        target = AuditTarget("database_connection", str(connection.id))
        add_audit_event(self._uow, AuditEntry(actor, action, target, details), self._clock)


@dataclass(frozen=True, slots=True)
class _Fields:
    engine: DatabaseEngine
    host: str
    port: int
    database: str
    username: str
    tls_mode: TlsMode
    network: str | None
    auto_detected: bool


def _validated_fields(new: NewConnection) -> _Fields:
    return _Fields(
        engine=new.engine,
        host=validate_host(new.host),
        port=validate_port(new.port if new.port is not None else DEFAULT_PORTS[new.engine]),
        database=validate_database_name(new.database),
        username=validate_username(new.username),
        tls_mode=new.tls_mode,
        network=new.network,
        auto_detected=new.auto_detected,
    )


def _validated_change(change: ConnectionChange) -> ConnectionChange:
    return ConnectionChange(
        engine=change.engine,
        host=validate_host(change.host) if change.host is not None else None,
        port=validate_port(change.port) if change.port is not None else None,
        database=validate_database_name(change.database) if change.database is not None else None,
        username=validate_username(change.username) if change.username is not None else None,
        password=None,
        tls_mode=change.tls_mode,
    )


def _changed_fields(change: ConnectionChange) -> set[str]:
    names = ("engine", "host", "port", "database", "username", "tls_mode")
    return {name for name in names if getattr(change, name) is not None}


def _apply(connection: DatabaseConnection, change: ConnectionChange) -> None:
    for name in _changed_fields(change):
        setattr(connection, name, getattr(change, name))


def _require(uow: UnitOfWork, environment: Environment, name: str) -> DatabaseConnection:
    connection = uow.database_connections.get(environment.id, name)
    if connection is None:
        raise NotFoundError(f"database connection not found: {name}")
    return connection
