"""Worker-side discovery and creation of connections for database containers."""

from __future__ import annotations

import re
from dataclasses import dataclass

from backend.adapters.container_admin import ContainerEngine
from backend.adapters.docker_inspect import parse_env, service_of
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
from backend.domain.database_detection import parse_database_credentials
from backend.domain.enums import EnvironmentName
from backend.domain.errors import ConflictError
from backend.repositories.database import UnitOfWork
from backend.repositories.models import DatabaseConnection
from backend.services.audit import add_audit_event
from backend.services.database_connections import DatabaseConnectionService, NewConnection
from backend.services.database_inspection import InspectionService
from backend.services.lookup import require_environment


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


class DatabaseDetectionService:
    """Detect, configure and test database connections entirely in the worker."""

    def __init__(
        self,
        uow: UnitOfWork,
        secrets: SecretsGateway,
        containers: ContainerEngine,
        inspection: InspectionService,
    ) -> None:
        self._uow, self._secrets, self._containers, self._inspection = (
            uow,
            secrets,
            containers,
            inspection,
        )

    def detect(self, slug: str, env: EnvironmentName, actor: Actor) -> DetectionSummary:
        """Create missing database connections, re-test existing ones, and audit summary."""
        with self._uow:
            environment = require_environment(self._uow, slug, env)
            project = environment.compose_project
            if not project:
                raise ConflictError("this environment has no compose project configured")
            environment_id = environment.id
            existing_rows = self._uow.database_connections.list_for_environment(environment_id)
        existing = list(existing_rows)
        results: list[DetectionResult] = []
        records = self._containers.list_project(project)
        for record in records:
            raw_state = record.get("State")
            state = raw_state if isinstance(raw_state, dict) else {}
            if not state.get("Running"):
                continue
            service = service_of(record)
            raw_config = record.get("Config")
            config = raw_config if isinstance(raw_config, dict) else {}
            image = str(config.get("Image") or "")
            if not service:
                continue
            parsed = parse_database_credentials(image, parse_env(record))
            if parsed is None:
                continue
            container = str(record.get("Id") or "")
            if parsed.credentials is None:
                results.append(
                    DetectionResult(
                        service, container, image, None, "skipped", parsed.reason, None, None
                    )
                )
                continue
            credentials = parsed.credentials
            match = next(
                (
                    item
                    for item in existing
                    if item.host == service and item.engine is credentials.engine
                ),
                None,
            )
            if match is not None:
                outcome = self._inspection.test(ConnectionRef(slug, env, match.name), actor)
                results.append(
                    DetectionResult(
                        service,
                        container,
                        image,
                        credentials.engine.value,
                        "existing",
                        None,
                        match,
                        outcome,
                    )
                )
                continue
            if len(existing) >= MAX_CONNECTIONS_PER_ENVIRONMENT:
                results.append(
                    DetectionResult(
                        service,
                        container,
                        image,
                        credentials.engine.value,
                        "skipped",
                        "maximum number of database connections reached",
                        None,
                        None,
                    )
                )
                continue
            name = _available_name(service, {item.name for item in existing})
            connection = DatabaseConnectionService(self._uow, self._secrets).create(
                slug,
                env,
                NewConnection(
                    name,
                    credentials.engine,
                    service,
                    credentials.database,
                    credentials.username,
                    credentials.password,
                ),
                actor,
            )
            existing.append(connection)
            outcome = self._inspection.test(ConnectionRef(slug, env, name), actor)
            results.append(
                DetectionResult(
                    service,
                    container,
                    image,
                    credentials.engine.value,
                    "created",
                    None,
                    connection,
                    outcome,
                )
            )
        with self._uow:
            environment = require_environment(self._uow, slug, env)
            add_audit_event(
                self._uow,
                AuditEntry(
                    actor,
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
                    },
                ),
            )
        return DetectionSummary(len(results), tuple(results))


def to_wire(summary: DetectionSummary, env: EnvironmentName) -> dict[str, object]:
    """Serialize safe detection metadata without crossing API schema boundaries."""
    return {
        "found": summary.found,
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
