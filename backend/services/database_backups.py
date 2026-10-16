"""Backup configuration and record lifecycle shared by API and worker."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

from backend.domain.actor import Actor
from backend.domain.audit import AuditEntry, AuditTarget
from backend.domain.clock import Clock, utcnow
from backend.domain.database_backups import BackupSettings
from backend.domain.errors import ConflictError, NotFoundError, ValidationError, WorkerError
from backend.repositories.database import UnitOfWork
from backend.repositories.models import DatabaseBackup, PlatformSetting
from backend.services.audit import add_audit_event

PREFIX = "database_backups."
BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
SECRET_NAME = "s3_backup_secret_key"  # noqa: S105 - stored secret identifier, not a credential


@dataclass(frozen=True, slots=True)
class BackupView:
    record: DatabaseBackup
    project: str
    environment: str
    connection: str


class DatabaseBackupService:
    def __init__(self, uow: UnitOfWork, clock: Clock = utcnow) -> None:
        self._uow = uow
        self._clock = clock

    def settings(self) -> BackupSettings:
        with self._uow:
            values = {
                row.key.removeprefix(PREFIX): row.value
                for row in self._uow.platform.settings()
                if row.key.startswith(PREFIX)
            }
            configured = self._uow.platform.secret_metadata(SECRET_NAME) is not None
        return BackupSettings(
            access_key_id=values.get("access_key_id", ""),
            bucket=values.get("bucket", "aic-consultation"),
            region=values.get("region", "eu-west-3"),
            prefix=values.get("prefix", "backups").strip("/"),
            hour=int(values.get("hour", "3")),
            enabled=values.get("enabled", "false").lower() == "true",
            mysql_image=values.get("mysql_image", "mysql:8.4"),
            mariadb_image=values.get("mariadb_image", "mariadb:11.4"),
            postgres_image=values.get("postgres_image", "postgres:17"),
            secret_configured=configured,
        )

    def update_settings(self, settings: BackupSettings, actor: Actor) -> BackupSettings:
        _validate_settings(settings)
        values = {
            "access_key_id": settings.access_key_id,
            "bucket": settings.bucket,
            "region": settings.region,
            "prefix": settings.prefix.strip("/"),
            "hour": str(settings.hour),
            "enabled": str(settings.enabled).lower(),
            "mysql_image": settings.mysql_image,
            "mariadb_image": settings.mariadb_image,
            "postgres_image": settings.postgres_image,
        }
        changed: list[str] = []
        if settings.enabled and (
            not settings.access_key_id.strip() or not self.settings().secret_configured
        ):
            raise ValidationError("save an access key ID and secret key before enabling backups")
        with self._uow:
            for name, value in values.items():
                key = PREFIX + name
                row = self._uow.platform.get_setting(key)
                if row is None:
                    self._uow.platform.add_setting(
                        PlatformSetting(key=key, value=value, updated_at=self._clock())
                    )
                elif row.value != value:
                    row.value = value
                    row.updated_at = self._clock()
                else:
                    continue
                changed.append(name)
            add_audit_event(
                self._uow,
                AuditEntry(
                    actor,
                    "integration.s3_backup_settings_update",
                    AuditTarget("platform", "database_backups"),
                    {"changed": changed},
                ),
                self._clock,
            )
        return self.settings()

    def create(self, connection_id: int, environment_id: int, trigger: str) -> DatabaseBackup:
        now = self._clock()
        with self._uow:
            if trigger == "manual" and self._uow.database_backups.has_running_for_connection(
                connection_id
            ):
                raise ConflictError("a backup is already running for this connection")
            return self._uow.database_backups.add(
                DatabaseBackup(
                    connection_id=connection_id,
                    environment_id=environment_id,
                    trigger=trigger,
                    status="running",
                    started_at=now,
                )
            )

    def start_manual(
        self,
        connection_id: int,
        environment_id: int,
        actor: Actor,
        connection: str,
        start_worker: Callable[[int], object],
    ) -> DatabaseBackup:
        """Create a manual record, start its worker, and safely fail it if dispatch fails."""
        row = self.create(connection_id, environment_id, "manual")
        try:
            start_worker(row.id)
        except WorkerError as error:
            self.finish(
                row.id,
                "failed",
                error="the worker could not start the backup",
                actor=actor,
                connection=connection,
            )
            raise error
        return row

    def fail_interrupted(self) -> list[int]:
        with self._uow:
            rows = self._uow.database_backups.interrupted()
            ids = [row.id for row in rows]
            for row in rows:
                row.status = "failed"
                row.finished_at = self._clock()
                row.error = "interrupted by a worker restart"
                add_audit_event(
                    self._uow,
                    AuditEntry(
                        Actor.system("database-backups"),
                        "database.backup_failed",
                        AuditTarget("database_backup", str(row.id)),
                        {"connection": "", "key": "", "size_bytes": None},
                    ),
                    self._clock,
                )
        return ids

    def finish(  # noqa: PLR0913 - one optional audit/result field each
        self,
        backup_id: int,
        status: str,
        *,
        size_bytes: int | None = None,
        object_key: str | None = None,
        error: str | None = None,
        actor: Actor | None = None,
        connection: str | None = None,
    ) -> DatabaseBackup:
        with self._uow:
            row = self._uow.database_backups.get(backup_id)
            if row is None:
                raise NotFoundError("backup record not found")
            row.status = status
            row.finished_at = self._clock()
            row.size_bytes = size_bytes
            row.object_key = object_key
            row.error = _sanitize_error(error) if error else None
            if status in ("succeeded", "failed"):
                action = (
                    "database.backup_succeeded"
                    if status == "succeeded"
                    else "database.backup_failed"
                )
                add_audit_event(
                    self._uow,
                    AuditEntry(
                        actor or Actor.system("database-backups"),
                        action,
                        AuditTarget("database_backup", str(row.id)),
                        {
                            "connection": connection or "",
                            "key": object_key or "",
                            "size_bytes": size_bytes,
                            **({"error": _sanitize_error(error)} if error else {}),
                        },
                    ),
                    self._clock,
                )
            return row

    def record_cleanup_failure(
        self,
        backup_id: int,
        message: str,
        *,
        actor: Actor,
        connection: str,
    ) -> DatabaseBackup:
        """Record retention trouble after a verified backup without downgrading its success."""
        sanitized = _sanitize_error(message)
        with self._uow:
            row = self._uow.database_backups.get(backup_id)
            if row is None:
                raise NotFoundError("backup record not found")
            row.error = sanitized
            add_audit_event(
                self._uow,
                AuditEntry(
                    actor,
                    "database.backup_failed",
                    AuditTarget("database_backup", str(row.id)),
                    {
                        "connection": connection,
                        "key": row.object_key or "",
                        "size_bytes": row.size_bytes,
                        "error": sanitized,
                    },
                ),
                self._clock,
            )
            return row

    def list(self, slug: str | None = None, env: str | None = None) -> list[BackupView]:
        with self._uow:
            return [BackupView(*row) for row in self._uow.database_backups.list(slug, env)]

    def get(self, backup_id: int) -> DatabaseBackup:
        with self._uow:
            row = self._uow.database_backups.get(backup_id)
            if row is None:
                raise NotFoundError("backup record not found")
            return row

    def delete_connection_records(self, connection_id: int) -> None:
        with self._uow:
            self._uow.database_backups.delete_for_connection(connection_id)


def _validate_settings(settings: BackupSettings) -> None:
    if not BUCKET.fullmatch(settings.bucket):
        raise ValidationError("bucket must be a valid S3 bucket name")
    if not settings.region or len(settings.region) > 64:
        raise ValidationError("enter a valid AWS region")
    if not 0 <= settings.hour <= 23:
        raise ValidationError("schedule hour must be from 0 to 23")
    if len(settings.prefix) > 180 or any(
        part in {".", ".."} for part in settings.prefix.split("/")
    ):
        raise ValidationError("prefix is invalid")
    for image in (settings.mysql_image, settings.mariadb_image, settings.postgres_image):
        if not image or len(image) > 255 or any(char.isspace() for char in image):
            raise ValidationError("dump client image is invalid")


def _sanitize_error(value: str) -> str:
    return " ".join(value.split())[:500]
