"""Worker-side discovery and creation of connections for database containers."""

from __future__ import annotations

import re
from _thread import LockType
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from typing import Protocol

from backend.adapters.container_admin import ContainerEngine, Record
from backend.adapters.docker_inspect import depends_on_of, networks_of, parse_env, service_of
from backend.adapters.worker_client import SecretsGateway
from backend.domain.actor import Actor
from backend.domain.audit import AuditEntry, AuditTarget
from backend.domain.database import (
    MAX_CONNECTIONS_PER_ENVIRONMENT,
    ConnectionRef,
    DatabaseEngine,
    password_secret_name,
    validate_connection_name,
)
from backend.domain.database_detection import (
    CredentialResult,
    DetectedCredentials,
    DetectionContainer,
    choose_database_network,
    parse_database_credentials,
    scope_database_services,
)
from backend.domain.enums import EnvironmentName
from backend.domain.errors import ConflictError
from backend.repositories.database import UnitOfWork
from backend.repositories.models import DatabaseConnection
from backend.services.audit import add_audit_event
from backend.services.database_connections import DatabaseConnectionService, NewConnection
from backend.services.lookup import require_environment


def _same_engine_family(left: DatabaseEngine, right: DatabaseEngine) -> bool:
    mysql_family = {DatabaseEngine.MYSQL, DatabaseEngine.MARIADB}
    return left is right or (left in mysql_family and right in mysql_family)


class ConnectionTester(Protocol):
    """Port used to test a created or existing connection."""

    def test(self, ref: ConnectionRef, actor: Actor) -> dict[str, object]:
        """Return the safe connection test result."""


class DetectionLockRegistry:
    """Provide one detection lock per project environment."""

    def __init__(self) -> None:
        self._registry_lock = Lock()
        self._locks: dict[tuple[str, EnvironmentName], LockType] = {}

    @contextmanager
    def for_environment(self, slug: str, env: EnvironmentName) -> Iterator[None]:
        """Serialize detections for one environment, leaving other environments independent."""
        key = (slug, env)
        with self._registry_lock:
            lock = self._locks.setdefault(key, Lock())
        with lock:
            yield


@dataclass(frozen=True, slots=True)
class DetectionResult:
    """Safe result for one detected database service."""

    service: str
    container: str
    image: str
    engine: str | None
    status: str
    reason: str | None
    connection: DatabaseConnection | None
    test: dict[str, object] | None


@dataclass(frozen=True, slots=True)
class DetectionSummary:
    """Typed result of a complete environment scan."""

    found: int
    results: tuple[DetectionResult, ...]
    removed: tuple[str, ...] = ()
    kept: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True, slots=True)
class _ConnectionCandidate:
    """Parsed credentials and identity for one compose service."""

    service: str
    name: str
    credentials: DetectedCredentials
    network: str | None


@dataclass(slots=True)
class _DetectionContext:
    """Mutable state scoped to one serialized environment detection."""

    slug: str
    project: str
    env: EnvironmentName
    actor: Actor
    environment_id: int
    app_services: tuple[str, ...]
    existing: list[DatabaseConnection]


@dataclass(frozen=True, slots=True)
class _InspectedContainer:
    """One project container and its safely parsed database credentials, if any."""

    record: Record
    service: str | None
    parsed: CredentialResult | None


class DatabaseDetectionService:
    """Detect, configure and test database connections entirely in the worker."""

    def __init__(
        self,
        uow: UnitOfWork,
        secrets: SecretsGateway,
        containers: ContainerEngine,
        inspection: ConnectionTester,
        lock_registry: DetectionLockRegistry | None = None,
    ) -> None:
        self._uow, self._secrets, self._containers, self._inspection = (
            uow,
            secrets,
            containers,
            inspection,
        )
        self._lock_registry = lock_registry or DetectionLockRegistry()

    def detect(
        self, slug: str, env: EnvironmentName, actor: Actor, *, prune: bool = False
    ) -> DetectionSummary:
        """Create missing database connections, re-test existing ones, and audit summary."""
        with self._lock_registry.for_environment(slug, env):
            return self._detect_locked(slug, env, actor, prune=prune)

    def _detect_locked(
        self, slug: str, env: EnvironmentName, actor: Actor, *, prune: bool
    ) -> DetectionSummary:
        context = self._context(slug, env, actor)
        records = self._containers.list_project(context.project)
        inspected = tuple(
            _InspectedContainer(record, service_of(record), _parse_container(record))
            for record in records
        )
        inventory = tuple(
            DetectionContainer(
                item.service,
                networks_of(item.record),
                depends_on_of(item.record),
                item.parsed is not None,
            )
            for item in inspected
            if item.service
        )
        scoped = scope_database_services(inventory, context.app_services)
        results = self._detect_running(context, inventory, scoped, inspected)
        # A torn-down project lists no containers; never treat that as "nothing in scope".
        removed, kept = self._prune(context, scoped) if prune and records else ([], [])
        self._audit_detection(context, results, removed)
        return DetectionSummary(len(results), tuple(results), tuple(removed), tuple(kept))

    def _context(self, slug: str, env: EnvironmentName, actor: Actor) -> _DetectionContext:
        with self._uow:
            environment = require_environment(self._uow, slug, env)
            if not environment.compose_project:
                raise ConflictError("this environment has no compose project configured")
            rows = self._uow.database_connections.list_for_environment(environment.id)
            return _DetectionContext(
                slug,
                environment.compose_project,
                env,
                actor,
                environment.id,
                tuple(environment.services or ()),
                list(rows),
            )

    def _detect_running(
        self,
        context: _DetectionContext,
        inventory: tuple[DetectionContainer, ...],
        scoped: frozenset[str],
        inspected: tuple[_InspectedContainer, ...],
    ) -> list[DetectionResult]:
        results: list[DetectionResult] = []
        for item in inspected:
            if not _is_running(item.record) or item.service not in scoped or item.parsed is None:
                continue
            result, connection = self._detect_one(context, inventory, item)
            results.append(result)
            if connection is not None and all(
                item.id != connection.id for item in context.existing
            ):
                context.existing.append(connection)
        return results

    def _detect_one(
        self,
        context: _DetectionContext,
        inventory: tuple[DetectionContainer, ...],
        item: _InspectedContainer,
    ) -> tuple[DetectionResult, DatabaseConnection | None]:
        record, service, parsed = item.record, item.service, item.parsed
        if service is None or parsed is None:
            raise RuntimeError("database detection received an unqualified container")
        config = record.get("Config")
        image = str(config.get("Image") or "") if isinstance(config, dict) else ""
        container = str(record.get("Id") or "")
        if parsed.credentials is None:
            return DetectionResult(
                service, container, image, None, "skipped", parsed.reason, None, None
            ), None
        credentials = parsed.credentials
        container_info = next((row for row in inventory if row.service == service), None)
        network = (
            choose_database_network(container_info, inventory, context.app_services)
            if container_info
            else None
        )
        match = next(
            (
                row
                for row in context.existing
                if row.host == service and _same_engine_family(row.engine, credentials.engine)
            ),
            None,
        )
        if match is not None:
            self._set_network(context.environment_id, match, network)
            self._set_engine(context.environment_id, match, credentials.engine)
            outcome = self._inspection.test(
                ConnectionRef(context.slug, context.env, match.name), context.actor
            )
            return DetectionResult(
                service,
                container,
                image,
                credentials.engine.value,
                "existing",
                None,
                match,
                outcome,
            ), None
        if len(context.existing) >= MAX_CONNECTIONS_PER_ENVIRONMENT:
            return DetectionResult(
                service,
                container,
                image,
                credentials.engine.value,
                "skipped",
                "maximum number of database connections reached",
                None,
                None,
            ), None
        name = _available_name(service, {row.name for row in context.existing})
        connection, status, _ = self._create_or_reuse_connection(
            context.slug,
            context.env,
            _ConnectionCandidate(service, name, credentials, network),
            context.existing,
            context.actor,
        )
        outcome = self._inspection.test(
            ConnectionRef(context.slug, context.env, connection.name), context.actor
        )
        return DetectionResult(
            service, container, image, credentials.engine.value, status, None, connection, outcome
        ), connection

    def _set_network(
        self, environment_id: int, match: DatabaseConnection, network: str | None
    ) -> None:
        if match.network == network:
            return
        with self._uow:
            current = self._uow.database_connections.get(environment_id, match.name)
            if current is not None:
                current.network = network
                match.network = network

    def _set_engine(
        self, environment_id: int, match: DatabaseConnection, engine: DatabaseEngine
    ) -> None:
        if not match.auto_detected or match.engine is engine:
            return
        with self._uow:
            current = self._uow.database_connections.get(environment_id, match.name)
            if current is not None and current.auto_detected:
                current.engine = engine
                match.engine = engine

    def _prune(
        self, context: _DetectionContext, scoped: frozenset[str]
    ) -> tuple[list[str], list[tuple[str, str]]]:
        connections = DatabaseConnectionService(self._uow, self._secrets)
        removed: list[str] = []
        kept: list[tuple[str, str]] = []
        for connection in list(connections.list(context.slug, context.env)):
            if not connection.auto_detected or connection.host in scoped:
                continue
            with self._uow:
                protected = self._uow.database_backups.has_successful_for_connection(connection.id)
            if protected:
                kept.append((connection.name, "has backups"))
            else:
                connections.delete(context.slug, context.env, connection.name, context.actor)
                removed.append(connection.name)
        return removed, kept

    def _audit_detection(
        self,
        context: _DetectionContext,
        results: list[DetectionResult],
        removed: list[str],
    ) -> None:
        with self._uow:
            environment = require_environment(self._uow, context.slug, context.env)
            add_audit_event(
                self._uow,
                AuditEntry(
                    context.actor,
                    "database.detect",
                    AuditTarget("environment", str(environment.id)),
                    {
                        "found": len(results),
                        "created": [
                            r.connection.name
                            for r in results
                            if r.status == "created" and r.connection
                        ],
                        "existing": [
                            r.connection.name
                            for r in results
                            if r.status == "existing" and r.connection
                        ],
                        "skipped": [r.service for r in results if r.status == "skipped"],
                        "removed": removed,
                    },
                ),
            )

    def _create_or_reuse_connection(
        self,
        slug: str,
        env: EnvironmentName,
        candidate: _ConnectionCandidate,
        existing: list[DatabaseConnection],
        actor: Actor,
    ) -> tuple[DatabaseConnection, str, list[DatabaseConnection]]:
        """Create one service connection or recover if another writer created it first."""
        service, name, credentials = (
            candidate.service,
            candidate.name,
            candidate.credentials,
        )
        connections = DatabaseConnectionService(self._uow, self._secrets)
        try:
            created = connections.create(
                slug,
                env,
                NewConnection(
                    name,
                    credentials.engine,
                    service,
                    credentials.database,
                    credentials.username,
                    credentials.password,
                    network=candidate.network,
                    auto_detected=True,
                ),
                actor,
            )
        except ConflictError:
            refreshed = list(connections.list(slug, env))
            match = next(
                (
                    item
                    for item in refreshed
                    if item.host == service and _same_engine_family(item.engine, credentials.engine)
                ),
                None,
            )
            if match is None:
                raise
            self._set_engine(match.environment_id, match, credentials.engine)
            return match, "existing", refreshed
        return created, "created", [*existing, created]


def to_wire(summary: DetectionSummary, env: EnvironmentName) -> dict[str, object]:
    """Serialize safe detection metadata without crossing API schema boundaries."""
    return {
        "found": summary.found,
        "removed": list(summary.removed),
        "kept": [{"name": name, "reason": reason} for name, reason in summary.kept],
        "results": [
            {
                "service": item.service,
                "container": item.container,
                "image": item.image,
                "engine": item.engine,
                "status": item.status,
                "reason": item.reason,
                "connection": _connection_wire(item.connection, env),
                "test": item.test,
            }
            for item in summary.results
        ],
    }


def _connection_wire(
    connection: DatabaseConnection | None, env: EnvironmentName
) -> dict[str, object] | None:
    if connection is None:
        return None
    return {
        "name": connection.name,
        "engine": DatabaseEngine(connection.engine).value,
        "host": connection.host,
        "port": connection.port,
        "database": connection.database,
        "username": connection.username,
        "tls_mode": connection.tls_mode.value,
        "network": connection.network,
        "auto_detected": connection.auto_detected,
        "password_secret": password_secret_name(connection.name),
        "read_only": True,
        "environment": env.value,
        "notice": (
            "Production connection: sessions are opened read-only, but the strongest guarantee "
            "is the "
            "database user itself. Use a dedicated user that only has SELECT privileges."
            if env is EnvironmentName.PRODUCTION
            else None
        ),
        "created_at": connection.created_at.isoformat(),
        "updated_at": connection.updated_at.isoformat(),
        "last_tested_at": (
            connection.last_tested_at.isoformat() if connection.last_tested_at else None
        ),
        "last_test_ok": connection.last_test_ok,
    }


def _available_name(service: str, used: set[str]) -> str:
    """Sanitize a compose service and allocate a deterministic free connection name."""
    base = re.sub(r"[^a-z0-9-]+", "-", service.lower()).strip("-") or "database"
    base = base[:48].rstrip("-") or "database"
    candidate = base
    suffix = 2
    while candidate in used:
        ending = f"-{suffix}"
        candidate = f"{base[: 48 - len(ending)].rstrip('-')}{ending}"
        suffix += 1
    return validate_connection_name(candidate)


def _is_running(record: Mapping[str, object]) -> bool:
    state = record.get("State")
    return isinstance(state, dict) and bool(state.get("Running"))


def _parse_container(record: Mapping[str, object]) -> CredentialResult | None:
    config = record.get("Config")
    image = str(config.get("Image") or "") if isinstance(config, dict) else ""
    return parse_database_credentials(image, parse_env(record))
