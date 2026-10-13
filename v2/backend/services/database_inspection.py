"""Database inspection use cases (worker side).

The worker is the only process that decrypts the connection password and the only one that can
start the helper container. Every operation resolves the connection and its password, runs one
helper container on the environment's compose network and translates the helper's failures into
domain errors. Console statements are audited with their text, never with results; reads of
production rows are audited with the table and page.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from backend.adapters.inspector_runner import HelperRequest
from backend.domain.actor import Actor
from backend.domain.audit import AuditEntry, AuditTarget
from backend.domain.clock import Clock, utcnow
from backend.domain.database import ConnectionRef, TableRef
from backend.domain.enums import EnvironmentName
from backend.domain.errors import (
    ConfigurationError,
    ConflictError,
    DomainError,
    NotFoundError,
    ValidationError,
    WorkerError,
)
from backend.inspector.model import ConnectionSpec, ErrorCode, InspectorError
from backend.inspector.sql_guard import validate_statement
from backend.repositories.database import UnitOfWork
from backend.repositories.models import DatabaseConnection
from backend.services.audit import add_audit_event
from backend.services.lookup import require_environment
from backend.services.secrets import SecretService

AUDIT_STATEMENT_CHARS = 4000
NETWORK_SUFFIX = "_default"


class HelperPort(Protocol):
    """Runs one helper operation on a docker network (``InspectorRunner``)."""

    def run(self, network: str, request: HelperRequest) -> dict[str, object]:
        """Run the request; raises ``InspectorError`` on failure."""


@dataclass(frozen=True, slots=True)
class _Target:
    connection_id: int
    environment_id: int
    environment: EnvironmentName
    name: str
    network: str | None
    password_secret: str
    spec: ConnectionSpec


class InspectionService:
    """Metadata, rows and console of one configured database connection."""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        secrets: Callable[[], SecretService],
        helper: HelperPort,
        clock: Clock = utcnow,
    ) -> None:
        self._uow_factory = uow_factory
        self._secrets = secrets
        self._helper = helper
        self._clock = clock

    # --- connection test ----------------------------------------------------------

    def test(self, ref: ConnectionRef, actor: Actor) -> dict[str, object]:
        """Connect and report; connection problems are reported, not raised."""
        target = self._target(ref)
        outcome: dict[str, object]
        try:
            server = self._invoke(target, "test", {})
            outcome = {"ok": True, "error": None, "server": server}
        except (DomainError, InspectorError) as error:
            code, message = _describe(error)
            outcome = {"ok": False, "error": {"code": code, "message": message}, "server": None}
        outcome["network"] = target.network
        self._record_test(target, actor, ok=bool(outcome["ok"]), outcome=outcome)
        return outcome

    def _record_test(
        self, target: _Target, actor: Actor, *, ok: bool, outcome: dict[str, object]
    ) -> None:
        uow = self._uow_factory()
        with uow:
            connection = uow.session.get(DatabaseConnection, target.connection_id)
            if connection is not None:
                connection.last_tested_at = self._clock()
                connection.last_test_ok = ok
            error = outcome.get("error")
            code = error.get("code") if isinstance(error, dict) else None
            details: dict[str, object] = {"name": target.name, "ok": ok, "error_code": code}
            self._audit(uow, actor, "database.connection.test", target, details)

    # --- metadata -----------------------------------------------------------------

    def schemas(self, ref: ConnectionRef, *, include_system: bool) -> dict[str, object]:
        """Schemas of the database."""
        target = self._target(ref)
        return self._run(target, "schemas", {"include_system": include_system})

    def tables(self, ref: ConnectionRef, schema: str) -> dict[str, object]:
        """Tables of a schema with approximate row counts."""
        target = self._target(ref)
        return self._run(target, "tables", {"schema": schema})

    def table(self, ref: TableRef) -> dict[str, object]:
        """Columns, indexes and foreign keys of a table."""
        target = self._target(ref.connection)
        return self._run(target, "table", {"schema": ref.schema, "table": ref.table})

    # --- rows ---------------------------------------------------------------------

    def rows(self, ref: TableRef, page: dict[str, object], actor: Actor) -> dict[str, object]:
        """One page of rows; reads of production data are audited."""
        target = self._target(ref.connection)
        args: dict[str, object] = {"schema": ref.schema, "table": ref.table, **page}
        if target.environment is EnvironmentName.PRODUCTION:
            details: dict[str, object] = {
                "name": target.name,
                "schema": ref.schema,
                "table": ref.table,
                "limit": page.get("limit"),
                "keyset": page.get("after") is not None,
                "offset": page.get("offset"),
            }
            self._audit_now(actor, "database.rows.read", target, details)
        return self._run(target, "rows", args)

    # --- console ------------------------------------------------------------------

    def query(self, ref: ConnectionRef, statement: str, actor: Actor) -> dict[str, object]:
        """Run a read-only statement. The statement text is audited, the results never."""
        target = self._target(ref)
        try:
            validated = validate_statement(statement, target.spec.engine)
        except InspectorError as error:
            self._audit_query(target, actor, statement, "rejected", None)
            raise ValidationError(error.message) from error
        try:
            result = self._invoke(target, "query", {"statement": validated.text})
        except InspectorError as error:
            self._audit_query(target, actor, statement, error.code.value, None)
            raise _translate(error) from error
        except DomainError:
            self._audit_query(target, actor, statement, "unavailable", None)
            raise
        self._audit_query(target, actor, statement, "ok", result)
        return result

    def _audit_query(
        self,
        target: _Target,
        actor: Actor,
        statement: str,
        outcome: str,
        result: dict[str, object] | None,
    ) -> None:
        count = result.get("row_count") if result is not None else None
        details: dict[str, object] = {
            "name": target.name,
            "statement": statement[:AUDIT_STATEMENT_CHARS],
            "statement_cut": len(statement) > AUDIT_STATEMENT_CHARS,
            "outcome": outcome,
            "rows": count,
            "truncated": bool(result.get("truncated")) if result is not None else None,
        }
        self._audit_now(actor, "database.query", target, details)

    # --- plumbing -----------------------------------------------------------------

    def _target(self, ref: ConnectionRef) -> _Target:
        uow = self._uow_factory()
        with uow:
            environment = require_environment(uow, ref.slug, ref.env)
            connection = uow.database_connections.get(environment.id, ref.name)
            if connection is None:
                raise NotFoundError(f"database connection not found: {ref.name}")
            network = (
                f"{environment.compose_project}{NETWORK_SUFFIX}"
                if environment.compose_project
                else None
            )
            return _Target(
                connection_id=connection.id,
                environment_id=environment.id,
                environment=environment.name,
                name=connection.name,
                network=network,
                password_secret=connection.password_secret,
                spec=ConnectionSpec(
                    engine=connection.engine,
                    host=connection.host,
                    port=connection.port,
                    database=connection.database,
                    username=connection.username,
                    password="",
                    tls=connection.tls_mode,
                ),
            )

    def _run(self, target: _Target, operation: str, args: dict[str, object]) -> dict[str, object]:
        try:
            return self._invoke(target, operation, args)
        except InspectorError as error:
            raise _translate(error) from error

    def _invoke(
        self, target: _Target, operation: str, args: dict[str, object]
    ) -> dict[str, object]:
        """Run one helper operation; raises ``InspectorError`` or a domain error."""
        if target.network is None:
            raise ConflictError(
                "the environment has no compose project, so there is no network to connect through"
            )
        password = self._secrets().resolve_for_run(target.environment_id, [target.password_secret])
        spec = ConnectionSpec(
            engine=target.spec.engine,
            host=target.spec.host,
            port=target.spec.port,
            database=target.spec.database,
            username=target.spec.username,
            password=password[target.password_secret],
            tls=target.spec.tls,
        )
        return self._helper.run(target.network, HelperRequest(operation, args, spec))

    def _audit_now(
        self, actor: Actor, action: str, target: _Target, details: dict[str, object]
    ) -> None:
        uow = self._uow_factory()
        with uow:
            self._audit(uow, actor, action, target, details)

    def _audit(
        self,
        uow: UnitOfWork,
        actor: Actor,
        action: str,
        target: _Target,
        details: dict[str, object],
    ) -> None:
        entry = AuditEntry(
            actor, action, AuditTarget("database_connection", str(target.connection_id)), details
        )
        add_audit_event(uow, entry, self._clock)


def _translate(error: InspectorError) -> DomainError:
    code = error.code
    if code in {
        ErrorCode.REJECTED,
        ErrorCode.BAD_REQUEST,
        ErrorCode.QUERY_FAILED,
        ErrorCode.TIMEOUT,
    }:
        return ValidationError(error.message)
    if code is ErrorCode.NOT_FOUND:
        return NotFoundError(error.message)
    if code in {ErrorCode.CONNECT_FAILED, ErrorCode.NETWORK_DENIED}:
        return ConflictError(error.message)
    return WorkerError(error.message)


def _describe(error: DomainError | InspectorError) -> tuple[str, str]:
    """A stable code and a user-safe message for a failed connection test."""
    if isinstance(error, InspectorError):
        return error.code.value, error.message
    if isinstance(error, ConflictError):
        return "network_unavailable", str(error)
    if isinstance(error, NotFoundError):
        return "secret_missing", str(error)
    if isinstance(error, ConfigurationError):
        return "helper_unavailable", str(error)
    return "failed", str(error)
