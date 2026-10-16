"""RPC handlers and daily loop for S3 database backups."""

from __future__ import annotations

import secrets
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import cast

import boto3

from backend.adapters.dump_runner import DumpRequest, DumpRunner, DumpRuntime
from backend.adapters.s3_store import S3Client, S3Store
from backend.domain.actor import Actor
from backend.domain.database import DatabaseEngine
from backend.domain.database_backups import CAIRO, BackupError, BackupSettings, next_run, object_key
from backend.domain.errors import UpstreamError
from backend.domain.notifications import EventPayload, NotificationEvent
from backend.repositories.database import UnitOfWork
from backend.repositories.models import DatabaseConnection
from backend.services.database_backups import SECRET_NAME, DatabaseBackupService
from backend.services.database_inspection import NETWORK_SUFFIX
from backend.services.platform_secrets import PlatformSecretService
from backend.services.secrets import SecretService
from backend.worker.rpc import BadRequestError, Handler, RpcCall


class BackupLocks:
    def __init__(self) -> None:
        self._lock = threading.Lock()

    def global_lock(self) -> threading.Lock:
        return self._lock


class DatabaseBackupHandlers:
    def __init__(  # noqa: PLR0913, PLR0917 - worker dependencies are independent ports
        self,
        uow_factory: Callable[[], UnitOfWork],
        runtime: DumpRuntime,
        platform_secrets: Callable[[], PlatformSecretService],
        secret_service: Callable[[], SecretService],
        notify: Callable[[EventPayload], int],
        locks: BackupLocks,
    ) -> None:
        self._uow_factory = uow_factory
        self._runtime = runtime
        self._platform_secrets = platform_secrets
        self._secret_service = secret_service
        self._notify = notify
        self._locks = locks

    def run(self, call: RpcCall) -> object:
        backup_id = call.params.get("backup_id")
        if isinstance(backup_id, bool) or not isinstance(backup_id, int) or backup_id < 1:
            raise BadRequestError("invalid backup record")
        actor = call.require_actor()
        thread = threading.Thread(
            target=self._run_record,
            args=(backup_id, actor),
            name=f"database-backup-{backup_id}",
            daemon=True,
        )
        thread.start()
        return {"started": True, "backup_id": backup_id}

    def set_s3_secret(self, call: RpcCall) -> object:
        value = call.params.get("secret_key")
        if not isinstance(value, str) or not value or len(value) > 512:
            raise BadRequestError("invalid S3 secret key")
        info = self._platform_secrets().set(SECRET_NAME, value, call.require_actor())
        return {"configured": info.configured}

    def clear_s3_secret(self, call: RpcCall) -> object:
        self._platform_secrets().clear(SECRET_NAME, call.require_actor())
        return {"configured": False}

    def test_s3(self, call: RpcCall) -> object:
        call.require_actor()
        settings = DatabaseBackupService(self._uow_factory()).settings()
        secret = self._platform_secrets().resolve(SECRET_NAME)
        if not settings.access_key_id or not secret:
            return {"ok": False, "message": "Enter and save S3 credentials before testing."}
        try:
            store = S3Store(
                cast(
                    S3Client,
                    boto3.client(
                        "s3",
                        region_name=settings.region,
                        aws_access_key_id=settings.access_key_id,
                        aws_secret_access_key=secret,
                    ),
                ),
                settings.bucket,
            )
            store.test(settings.prefix, secrets.token_hex(12))
        except Exception:
            return {
                "ok": False,
                "message": (
                    "S3 connection test failed. Check the bucket, region and IAM permissions."
                ),
            }
        return {
            "ok": True,
            "message": "S3 connection verified; the temporary test object was deleted.",
        }

    def run_scheduled(self) -> None:
        settings = DatabaseBackupService(self._uow_factory()).settings()
        if not settings.enabled or not settings.secret_configured or not settings.access_key_id:
            return
        with self._uow_factory() as uow:
            eligible = list(uow.database_backups.eligible())
        for _project, environment, connection in eligible:
            record = DatabaseBackupService(self._uow_factory()).create(
                connection.id, environment.id, "schedule"
            )
            self._run_record(record.id, Actor.system("database-backups"))

    def schedule_forever(self, shutdown: object) -> None:
        while not getattr(shutdown, "requested", False):
            settings = DatabaseBackupService(self._uow_factory()).settings()
            now = datetime.now(UTC)
            local_now = now.astimezone(CAIRO)
            local_start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            local_end = local_start + timedelta(days=1)
            with self._uow_factory() as uow:
                already_ran = uow.database_backups.scheduled_on(local_start, local_end)
            if settings.enabled and not already_ran and local_now.hour >= settings.hour:
                self.run_scheduled()
            target = next_run(now, settings.hour)
            wait = max(1, min(60, int((target - now).total_seconds())))
            if shutdown.wait(wait):  # type: ignore[attr-defined]
                return

    def _run_record(self, backup_id: int, actor: Actor) -> None:  # noqa: PLR0915
        with self._uow_factory() as uow:
            record = uow.database_backups.get(backup_id)
            if record is None:
                return
            connection = uow.session.get(DatabaseConnection, record.connection_id)
            environment = uow.environments.get(record.environment_id)
            project = uow.projects.get(environment.project_id) if environment else None
            snapshot = (record, connection, environment, project)
        record, connection, environment, project = snapshot
        if connection is None or environment is None or project is None:
            self._failed(
                record.id,
                actor,
                "database connection is unavailable",
                project.slug if project else "",
                environment.name.value if environment else "",
                "",
            )
            return
        lock = self._locks.global_lock()
        lock.acquire()
        key = ""
        size: int | None = None
        verified = False
        try:
            settings = DatabaseBackupService(self._uow_factory()).settings()
            secret = self._platform_secrets().resolve(SECRET_NAME) or ""
            if not secret or not settings.access_key_id:
                raise BackupError("S3 backup credentials are not configured")
            secret_value = (
                self._secret_service()
                .resolve_for_run(environment.id, [connection.password_secret])
                .get(connection.password_secret)
            )
            if not secret_value:
                raise BackupError("database password is not configured")
            key = object_key(
                settings.prefix,
                project.slug,
                environment.name.value,
                connection.name,
                record.started_at,
                connection.engine is DatabaseEngine.POSTGRESQL,
            )
            client = boto3.client(
                "s3",
                region_name=settings.region,
                aws_access_key_id=settings.access_key_id,
                aws_secret_access_key=secret,
            )
            store = S3Store(cast(S3Client, client), settings.bucket)
            runner = DumpRunner(self._runtime, store)
            image = _image(settings, connection.engine)
            if not environment.compose_project:
                raise BackupError("environment compose project is not configured")
            network = f"{environment.compose_project}{NETWORK_SUFFIX}"
            runner.run(
                DumpRequest(
                    engine=connection.engine,
                    image=image,
                    network=network,
                    host=connection.host,
                    port=connection.port,
                    database=connection.database,
                    username=connection.username,
                    password=secret_value,
                    key=key,
                )
            )
            prefix = (
                f"{settings.prefix.rstrip('/')}/{project.slug}/"
                f"{environment.name.value}/{connection.name}/"
            )
            size = store.size(key)
            if size <= 0:
                raise BackupError("S3 verification returned an empty object")
            verified = True
            DatabaseBackupService(self._uow_factory()).finish(
                record.id,
                "succeeded",
                size_bytes=size,
                object_key=key,
                actor=actor,
                connection=connection.name,
            )
            try:
                for previous in store.keys(prefix):
                    if previous != key:
                        store.delete(previous)
            except Exception:
                message = _safe_error(BackupError("previous backups could not be removed"))
                DatabaseBackupService(self._uow_factory()).record_cleanup_failure(
                    record.id,
                    message,
                    actor=actor,
                    connection=connection.name,
                )
                self._notify(
                    EventPayload(
                        event=NotificationEvent.BACKUP_FAILED,
                        dedupe_key=f"backup.failed:{record.id}:retention",
                        title=(
                            "Database backup succeeded, but retention cleanup failed: "
                            f"{project.slug}/{environment.name.value}/{connection.name}"
                        ),
                        message=message,
                        details={"connection": connection.name, "key": key},
                    )
                )
                return
        except Exception as error:
            sanitized = _safe_error(error)
            self._failed(
                record.id,
                actor,
                sanitized,
                project.slug,
                environment.name.value,
                connection.name,
                key if verified else None,
                size if verified else None,
            )
        finally:
            lock.release()

    def _failed(  # noqa: PLR0913, PLR0917 - failure notification needs its resource context
        self,
        backup_id: int,
        actor: Actor,
        message: str,
        project: str,
        environment: str,
        connection: str,
        key: str | None = None,
        size: int | None = None,
    ) -> None:
        DatabaseBackupService(self._uow_factory()).finish(
            backup_id,
            "failed",
            error=message,
            object_key=key,
            size_bytes=size,
            actor=actor,
            connection=connection,
        )
        self._notify(
            EventPayload(
                event=NotificationEvent.BACKUP_FAILED,
                dedupe_key=f"backup.failed:{backup_id}",
                title=f"Database backup failed: {project}/{environment}/{connection}",
                message=message,
                details={"connection": connection, "key": key or ""},
            )
        )


def _image(settings: BackupSettings, engine: DatabaseEngine) -> str:
    return {
        DatabaseEngine.MYSQL: settings.mysql_image,
        DatabaseEngine.MARIADB: settings.mariadb_image,
        DatabaseEngine.POSTGRESQL: settings.postgres_image,
    }[engine]


def _safe_error(error: Exception) -> str:
    if isinstance(error, BackupError):
        return str(error)[:500]
    if isinstance(error, UpstreamError):
        message = str(error)
        safe_s3_errors = {
            "S3 upload failed": "S3 upload failed",
            "S3 verification failed": "S3 verification failed",
            "S3 listing failed": "previous backups could not be removed",
            "S3 retention cleanup failed": "previous backups could not be removed",
        }
        if message in safe_s3_errors:
            return safe_s3_errors[message]
    return "database backup could not be completed"


def build_database_backup_handlers(handlers: DatabaseBackupHandlers) -> Mapping[str, Handler]:
    return {
        "database_backups.run": handlers.run,
        "database_backups.set_s3_secret": handlers.set_s3_secret,
        "database_backups.clear_s3_secret": handlers.clear_s3_secret,
        "database_backups.test_s3": handlers.test_s3,
    }
