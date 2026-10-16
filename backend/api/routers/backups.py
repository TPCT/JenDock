"""Backup history, manual runs and the administrator-only S3 integration settings."""

from __future__ import annotations

import contextlib
from typing import Annotated

from fastapi import APIRouter, Depends, Response

from backend.adapters.worker_client import WorkerClient
from backend.api.authz import authorize
from backend.api.deps import Container, Target, require
from backend.api.principal import Principal
from backend.api.schemas.database_backups import (
    BackupResponse,
    BackupSettingsRequest,
    BackupSettingsResponse,
)
from backend.domain.access import Action, Resource
from backend.domain.enums import EnvironmentName
from backend.domain.errors import NotFoundError, PermissionDeniedError, WorkerError

router = APIRouter(tags=["backups"])
BackupReader = Annotated[Principal, Depends(require(Action.BACKUP_READ))]
BackupRunner = Annotated[Principal, Depends(require(Action.BACKUP_RUN))]
IntegrationManager = Annotated[Principal, Depends(require(Action.INTEGRATION_MANAGE, Target.NONE))]
ProjectLister = Annotated[Principal, Depends(require(Action.PROJECT_LIST, Target.NONE))]


@router.get("/api/backups")
def list_all_backups(principal: ProjectLister, container: Container) -> list[BackupResponse]:
    """List records for projects visible to the caller (a project-list scope is required)."""
    rows = container.database_backups().list()
    result: list[BackupResponse] = []
    for view in rows:
        resource = Resource.of_environment(view.project, EnvironmentName(view.environment))
        try:
            authorize(principal, Action.BACKUP_READ, resource)
        except (NotFoundError, PermissionDeniedError):
            continue
        result.append(BackupResponse.from_view(view))
    return result


@router.get("/api/projects/{slug}/environments/{env}/backups")
def list_environment_backups(
    slug: str, env: EnvironmentName, _principal: BackupReader, container: Container
) -> list[BackupResponse]:
    return [
        BackupResponse.from_view(view)
        for view in container.database_backups().list(slug, env.value)
    ]


@router.post(
    "/api/projects/{slug}/environments/{env}/databases/{connection}/backups",
    status_code=202,
)
def run_backup(  # noqa: PLR0913, PLR0917 - route dependencies are independently injected
    slug: str,
    env: EnvironmentName,
    connection: str,
    principal: BackupRunner,
    container: Container,
    response: Response,
) -> BackupResponse:
    """Queue one manual backup and return its running record."""
    conn = container.database_connections().get(slug, env, connection)
    environment = container.environment_service().get(slug, env)
    service = container.database_backups()

    def start(backup_id: int) -> object:
        WorkerClient(container.settings.worker_socket_path, timeout=5).call(
            "database_backups.run", principal.actor, {"backup_id": backup_id}, timeout=5
        )
        return None

    row = service.start_manual(conn.id, environment.id, principal.actor, connection, start)
    response.headers["Location"] = f"/api/projects/{slug}/environments/{env.value}/backups"
    return BackupResponse.from_model(row, slug, env.value, connection)


@router.get("/api/integrations/s3-backups")
def get_s3_backup_settings(
    _principal: IntegrationManager, container: Container
) -> BackupSettingsResponse:
    return BackupSettingsResponse.from_settings(container.database_backups().settings())


@router.put("/api/integrations/s3-backups")
def update_s3_backup_settings(
    body: BackupSettingsRequest, principal: IntegrationManager, container: Container
) -> BackupSettingsResponse:
    service = container.database_backups()
    if body.secret_key:
        WorkerClient(container.settings.worker_socket_path).call(
            "database_backups.set_s3_secret", principal.actor, {"secret_key": body.secret_key}
        )
    settings = service.update_settings(body.to_settings(), principal.actor)
    with contextlib.suppress(WorkerError):
        WorkerClient(container.settings.worker_socket_path, timeout=5).call(
            "database_backups.warm_images", principal.actor, {}, timeout=5
        )
    return BackupSettingsResponse.from_settings(settings)


@router.post("/api/integrations/s3-backups/test")
def test_s3_backup_connection(
    principal: IntegrationManager, container: Container
) -> dict[str, object]:
    result = WorkerClient(container.settings.worker_socket_path, timeout=60).call(
        "database_backups.test_s3", principal.actor, {}, timeout=60
    )
    if not isinstance(result, dict):
        raise WorkerError("worker returned an unexpected response")
    return {"ok": bool(result.get("ok")), "message": str(result.get("message", ""))[:300]}
