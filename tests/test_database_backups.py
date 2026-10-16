from __future__ import annotations

import gzip
import logging
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from typing import BinaryIO, cast

import pytest

from backend.adapters.container_runtime import ExecResult, RunSpec
from backend.adapters.docker_args import env_file_text, run_argv
from backend.adapters.dump_runner import DumpRuntime
from backend.adapters.s3_store import S3Client, S3Store, verify_and_retain_latest
from backend.domain.actor import Actor
from backend.domain.database import DatabaseEngine, password_secret_name
from backend.domain.database_backups import CAIRO, BackupError, BackupSettings, next_run, object_key
from backend.domain.enums import EnvironmentName
from backend.domain.errors import ConflictError, UpstreamError, WorkerError
from backend.domain.notifications import EventPayload
from backend.repositories.database import UnitOfWork
from backend.repositories.models import AuditEvent, DatabaseBackup, DatabaseConnection
from backend.services.database_backups import SECRET_NAME, DatabaseBackupService
from backend.services.platform_secrets import PlatformSecretService
from backend.services.secret_cipher import SecretCipher
from backend.services.secrets import SecretService
from backend.worker.database_backup_handlers import BackupLocks, DatabaseBackupHandlers, _safe_error
from backend.worker.rpc import RpcCall
from tests.execution_support import FakeContainerRuntime

DB_MARKER = "fixture-db-" + secrets.token_hex(8)
S3_MARKER = "fixture-s3-" + secrets.token_hex(8)
FIXTURE_LOGIN = "fixture_" + "reader"
FIXTURE_HOST = "db"
FIXTURE_SCHEMA = "app"


def _connection_ref(name: str) -> str:
    return password_secret_name(name)


class FakeS3:
    def __init__(
        self,
        *,
        empty_head: bool = False,
        fail_upload: bool = False,
        fail_delete: bool = False,
    ) -> None:
        self.objects: dict[str, bytes] = {}
        self.upload_args: dict[str, object] | None = None
        self.empty_head = empty_head
        self.fail_upload = fail_upload
        self.fail_delete = fail_delete
        self.delete_calls: list[str] = []
        self.completed_uploads: list[str] = []

    def upload_fileobj(self, fileobj: BinaryIO, bucket: str, key: str, **kwargs: object) -> None:
        self.upload_args = kwargs
        if self.fail_upload:
            raise RuntimeError("upload failed")
        body = fileobj.read()
        self.objects[key] = body
        self.completed_uploads.append(key)

    def head_object(self, **kwargs: object) -> dict[str, object]:
        key = str(kwargs["Key"])
        return {"ContentLength": 0 if self.empty_head else len(self.objects[key])}

    def list_objects_v2(self, **kwargs: object) -> dict[str, object]:
        prefix = str(kwargs["Prefix"])
        return {"Contents": [{"Key": key} for key in self.objects if key.startswith(prefix)]}

    def delete_object(self, **kwargs: object) -> None:
        key = str(kwargs["Key"])
        self.delete_calls.append(key)
        if self.fail_delete:
            raise RuntimeError("delete failed")
        self.objects.pop(key, None)

    def put_object(self, **kwargs: object) -> None:
        body = kwargs["Body"]
        if isinstance(body, bytes):
            self.objects[str(kwargs["Key"])] = body


class FakeRuntime:
    def __init__(self, *, exit_code: int = 0) -> None:
        self.exit_code = exit_code
        self.spec: RunSpec | None = None
        self.kills: list[str] = []

    def image_exists(self, image: str) -> bool:
        return bool(image)

    def run_container_binary(
        self, spec: RunSpec, sink: Callable[[bytes], None], *, timeout: float | None = None
    ) -> ExecResult:
        self.spec = spec
        sink(b"CREATE TABLE example (id integer);\n")
        return ExecResult(self.exit_code)

    def kill_by_label(self, label: str) -> int:
        self.kills.append(label)
        return 0


def _handler(
    uow: UnitOfWork,
    cipher: SecretCipher,
    runtime: DumpRuntime,
    *,
    notify: Callable[[EventPayload], int] | None = None,
) -> DatabaseBackupHandlers:
    factory = uow._session_factory
    return DatabaseBackupHandlers(
        lambda: UnitOfWork(factory),
        runtime,
        lambda: PlatformSecretService(UnitOfWork(factory), cipher),
        lambda: SecretService(UnitOfWork(factory), cipher),
        notify or (lambda _payload: 0),
        BackupLocks(),
    )


def _configured(
    uow: UnitOfWork, cipher: SecretCipher, project: str, engine: DatabaseEngine
) -> DatabaseBackup:
    actor = Actor.system("test")
    with uow:
        env = uow.environments.get_by_project_slug(project, EnvironmentName.PRODUCTION)
        assert env is not None
        env.compose_project = f"{project}-production"
        connection = uow.database_connections.add(
            DatabaseConnection(
                environment_id=env.id,
                name="main",
                engine=engine,
                host=FIXTURE_HOST,
                port=5432 if engine is DatabaseEngine.POSTGRESQL else 3306,
                database=FIXTURE_SCHEMA,
                username=FIXTURE_LOGIN,
                password_secret=_connection_ref("main"),
            )
        )
        environment_id = env.id
        connection_id = connection.id
    SecretService(uow, cipher).set(
        project,
        EnvironmentName.PRODUCTION,
        _connection_ref("main"),
        DB_MARKER,
        actor,
    )
    PlatformSecretService(uow, cipher).set(SECRET_NAME, S3_MARKER, actor)
    DatabaseBackupService(uow).update_settings(
        BackupSettings(access_key_id="AKIAEXAMPLE", enabled=True), actor
    )
    return DatabaseBackupService(uow).create(connection_id, environment_id, "manual")


@pytest.mark.parametrize(
    ("db_engine", "extension"),
    [
        (DatabaseEngine.MYSQL, ".sql.gz"),
        (DatabaseEngine.MARIADB, ".sql.gz"),
        (DatabaseEngine.POSTGRESQL, ".dump"),
    ],
)
def test_worker_uploads_verified_object_and_retains_only_latest(
    uow: UnitOfWork,
    cipher: SecretCipher,
    project: str,
    monkeypatch: pytest.MonkeyPatch,
    db_engine: DatabaseEngine,
    extension: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    fake_s3 = FakeS3()

    def fake_client(*_args: object, **_kwargs: object) -> FakeS3:
        return fake_s3

    monkeypatch.setattr("backend.worker.database_backup_handlers.boto3.client", fake_client)
    row = _configured(uow, cipher, project, db_engine)
    runtime = FakeRuntime()
    handler = _handler(uow, cipher, runtime)
    prior = f"backups/{project}/production/main/2026-10-15{extension}"
    fake_s3.objects[prior] = b"old backup"

    handler._run_record(row.id, Actor.system("test"))

    record = DatabaseBackupService(uow).get(row.id)
    expected_prefix = f"backups/{project}/production/main/"
    assert record.status == "succeeded"
    assert record.object_key is not None and record.object_key.endswith(extension)
    local_started = row.started_at.astimezone(CAIRO).strftime("%Y-%m-%dT%H%M%S")
    assert record.object_key.endswith(f"/{local_started}{extension}")
    assert prior not in fake_s3.objects
    assert list(fake_s3.objects) == [record.object_key]
    assert fake_s3.upload_args == {"ExtraArgs": {"ServerSideEncryption": "AES256"}}
    assert runtime.spec is not None
    assert DB_MARKER not in runtime.spec.command
    assert S3_MARKER not in runtime.spec.command
    assert runtime.spec.env == {
        "PGPASSWORD" if db_engine is DatabaseEngine.POSTGRESQL else "MYSQL_PWD": DB_MARKER
    }
    argv = run_argv("docker", runtime.spec, {}, "/worker/env-files/backup.env")
    env_text = env_file_text(runtime.spec.env)
    assert "--env-file" in argv
    assert DB_MARKER not in argv
    assert S3_MARKER not in argv
    assert DB_MARKER in env_text
    assert not any(
        DB_MARKER in record.getMessage() or S3_MARKER in record.getMessage()
        for record in caplog.records
    )
    assert record.object_key.startswith(expected_prefix)
    if db_engine is not DatabaseEngine.POSTGRESQL:
        expected_dump = b"CREATE TABLE example (id integer);\n"
        assert gzip.decompress(fake_s3.objects[record.object_key]) == expected_dump
    with uow:
        audits = uow.session.query(AuditEvent).all()
        serialized = str([row.details for row in audits])
        assert DB_MARKER not in serialized
        assert S3_MARKER not in serialized
    fields = str({column.key: getattr(record, column.key) for column in record.__table__.columns})
    assert DB_MARKER not in fields
    assert S3_MARKER not in fields


@pytest.mark.parametrize(
    ("exit_code", "empty_head", "fail_upload"),
    [(0, True, False), (1, False, False), (0, False, True)],
)
def test_any_unverified_backup_fails_and_keeps_previous_object(
    uow: UnitOfWork,
    cipher: SecretCipher,
    project: str,
    monkeypatch: pytest.MonkeyPatch,
    exit_code: int,
    empty_head: bool,
    fail_upload: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    fake_s3 = FakeS3(empty_head=empty_head, fail_upload=fail_upload)

    def fake_client(*_args: object, **_kwargs: object) -> FakeS3:
        return fake_s3

    monkeypatch.setattr("backend.worker.database_backup_handlers.boto3.client", fake_client)
    row = _configured(uow, cipher, project, DatabaseEngine.POSTGRESQL)
    prior = f"backups/{project}/production/main/2026-10-15.dump"
    fake_s3.objects[prior] = b"old backup"
    same_day = object_key("backups", project, "production", "main", row.started_at, True)
    if fail_upload:
        fake_s3.objects[same_day] = b"previous same-day object"
    notified: list[object] = []

    def notify(payload: EventPayload) -> int:
        notified.append(payload)
        return 1

    handler = _handler(
        uow,
        cipher,
        FakeRuntime(exit_code=exit_code),
        notify=notify,
    )

    handler._run_record(row.id, Actor.system("test"))

    failed = DatabaseBackupService(uow).get(row.id)
    assert failed.status == "failed"
    assert failed.error is not None
    expected_error = (
        "S3 verification returned an empty object"
        if empty_head
        else "dump container exited with code 1"
        if exit_code
        else "S3 upload failed"
    )
    assert failed.error == expected_error
    assert prior in fake_s3.objects
    if fail_upload:
        assert fake_s3.objects[same_day] == b"previous same-day object"
    assert len(notified) == 1
    assert expected_error in str(notified[0])
    assert S3_MARKER not in failed.error
    assert DB_MARKER not in failed.error
    with uow:
        audits = uow.session.query(AuditEvent).all()
        serialized = str([audit.details for audit in audits])
    fields = str({column.key: getattr(failed, column.key) for column in failed.__table__.columns})
    for marker in (S3_MARKER, DB_MARKER):
        assert marker not in serialized
        assert marker not in fields
        assert all(marker not in record.getMessage() for record in caplog.records)
    if fail_upload:
        assert fake_s3.delete_calls == []


def test_schedule_time_and_environment_selection(uow: UnitOfWork, project: str) -> None:
    now = datetime(2026, 10, 16, 0, 0, tzinfo=UTC)
    assert next_run(now, 2) == datetime(2026, 10, 16, 23, 0, tzinfo=UTC)
    assert next_run(datetime(2026, 4, 23, 2, 0, tzinfo=UTC), 3) == datetime(
        2026, 4, 24, 0, 0, tzinfo=UTC
    )
    assert next_run(datetime(2026, 10, 29, 1, 0, tzinfo=UTC), 3) == datetime(
        2026, 10, 30, 1, 0, tzinfo=UTC
    )
    with uow:
        project_row = uow.projects.get_by_slug(project)
        assert project_row is not None
        environments = uow.environments.list_for_project(project_row.id)
        development = next(env for env in environments if env.name is EnvironmentName.DEVELOPMENT)
        production = next(env for env in environments if env.name is EnvironmentName.PRODUCTION)
        assert not development.include_in_backups
        assert production.name is EnvironmentName.PRODUCTION
        for environment in (development, production):
            uow.database_connections.add(
                DatabaseConnection(
                    environment_id=environment.id,
                    name="main",
                    engine=DatabaseEngine.POSTGRESQL,
                    host="db",
                    port=5432,
                    database="app",
                    username=FIXTURE_LOGIN,
                    password_secret="DBCONN_MAIN_PASSWORD",
                )
            )
    with uow:
        eligible = uow.database_backups.eligible()
    assert [environment.name for _, environment, _ in eligible] == [EnvironmentName.PRODUCTION]
    with uow:
        project_row = uow.projects.get_by_slug(project)
        assert project_row is not None
        development = next(
            env
            for env in uow.environments.list_for_project(project_row.id)
            if env.name is EnvironmentName.DEVELOPMENT
        )
        development.include_in_backups = True
    with uow:
        eligible = uow.database_backups.eligible()
    assert {environment.name for _, environment, _ in eligible} == {
        EnvironmentName.DEVELOPMENT,
        EnvironmentName.PRODUCTION,
    }


def test_schedule_record_prevents_same_day_rerun_after_restart(
    uow: UnitOfWork,
    cipher: SecretCipher,
    project: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_s3 = FakeS3()
    monkeypatch.setattr(
        "backend.worker.database_backup_handlers.boto3.client",
        lambda *_args, **_kwargs: fake_s3,
    )
    _configured(uow, cipher, project, DatabaseEngine.POSTGRESQL)
    handler = _handler(uow, cipher, FakeRuntime())

    class StopAfterCheck:
        requested = False

        @staticmethod
        def wait(_timeout: float) -> bool:
            return True

    handler.schedule_forever(StopAfterCheck())
    with uow:
        first_count = uow.session.query(DatabaseBackup).filter_by(trigger="schedule").count()
    assert first_count == 1

    handler.schedule_forever(StopAfterCheck())
    with uow:
        second_count = uow.session.query(DatabaseBackup).filter_by(trigger="schedule").count()
    assert second_count == first_count


def test_s3_test_connection_puts_lists_and_deletes() -> None:
    client = FakeS3()
    S3Store(cast(S3Client, client), "bucket").test("backups", "random")
    assert client.objects == {}


def test_s3_test_rpc_returns_generic_message_when_client_raises(
    uow: UnitOfWork,
    cipher: SecretCipher,
    project: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configured(uow, cipher, project, DatabaseEngine.POSTGRESQL)
    handler = _handler(uow, cipher, FakeRuntime())

    def fail_client(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError(S3_MARKER)

    monkeypatch.setattr("backend.worker.database_backup_handlers.boto3.client", fail_client)
    result = handler.test_s3(RpcCall(Actor.system("test"), {}))
    assert result == {
        "ok": False,
        "message": "S3 connection test failed. Check the bucket, region and IAM permissions.",
    }
    assert S3_MARKER not in str(result)
    assert DB_MARKER not in str(result)


def test_manual_creation_rejects_an_existing_running_record(
    uow: UnitOfWork,
    cipher: SecretCipher,
    project: str,
) -> None:
    running = _configured(uow, cipher, project, DatabaseEngine.POSTGRESQL)
    with pytest.raises(ConflictError, match="already running"):
        DatabaseBackupService(uow).create(running.connection_id, running.environment_id, "manual")


def test_manual_dispatch_failure_marks_the_record_failed(
    uow: UnitOfWork,
    cipher: SecretCipher,
    project: str,
) -> None:
    previous = _configured(uow, cipher, project, DatabaseEngine.POSTGRESQL)
    service = DatabaseBackupService(uow)
    service.finish(previous.id, "succeeded")

    def fail_dispatch(_backup_id: int) -> None:
        raise WorkerError("dispatch failed")

    with pytest.raises(WorkerError):
        service.start_manual(
            previous.connection_id,
            previous.environment_id,
            Actor.system("test"),
            "main",
            fail_dispatch,
        )
    failed = service.list(project, "production")
    assert next(view.record for view in failed if view.record.id != previous.id).status == "failed"


def test_worker_restart_marks_running_backup_failed(
    uow: UnitOfWork,
    cipher: SecretCipher,
    project: str,
) -> None:
    row = _configured(uow, cipher, project, DatabaseEngine.POSTGRESQL)
    service = DatabaseBackupService(uow)

    assert service.fail_interrupted() == [row.id]

    interrupted = service.get(row.id)
    assert interrupted.status == "failed"
    assert interrupted.error == "interrupted by a worker restart"


def test_retention_failure_keeps_verified_new_object(
    uow: UnitOfWork,
    cipher: SecretCipher,
    project: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_s3 = FakeS3(fail_delete=True)
    monkeypatch.setattr(
        "backend.worker.database_backup_handlers.boto3.client",
        lambda *_a, **_k: fake_s3,
    )
    row = _configured(uow, cipher, project, DatabaseEngine.POSTGRESQL)
    prior = f"backups/{project}/production/main/2026-10-15.dump"
    fake_s3.objects[prior] = b"old backup"
    notified: list[EventPayload] = []

    def notify(payload: EventPayload) -> int:
        notified.append(payload)
        return 1

    handler = _handler(uow, cipher, FakeRuntime(), notify=notify)

    handler._run_record(row.id, Actor.system("test"))

    succeeded = DatabaseBackupService(uow).get(row.id)
    assert succeeded.status == "succeeded"
    assert succeeded.error == "previous backups could not be removed"
    assert succeeded.object_key in fake_s3.objects
    assert prior in fake_s3.objects
    assert len(notified) == 1
    assert notified[0].title.startswith("Database backup succeeded, but retention cleanup failed:")
    with uow:
        audit = uow.session.query(AuditEvent).filter_by(action="database.backup_failed").one()
        assert audit.details["error"] == "previous backups could not be removed"
        assert audit.details["key"] == succeeded.object_key


def test_success_persistence_failure_does_not_delete_existing_objects(
    uow: UnitOfWork,
    cipher: SecretCipher,
    project: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_s3 = FakeS3()
    monkeypatch.setattr(
        "backend.worker.database_backup_handlers.boto3.client",
        lambda *_a, **_k: fake_s3,
    )
    row = _configured(uow, cipher, project, DatabaseEngine.POSTGRESQL)
    prior = f"backups/{project}/production/main/2026-10-15T030000.dump"
    fake_s3.objects[prior] = b"old backup"
    original_finish = DatabaseBackupService.finish

    def fail_success_persistence(
        self: DatabaseBackupService,
        backup_id: int,
        status: str,
        *,
        size_bytes: int | None = None,
        object_key: str | None = None,
        error: str | None = None,
        actor: Actor | None = None,
        connection: str | None = None,
    ) -> DatabaseBackup:
        if status == "succeeded":
            raise RuntimeError("database unavailable")
        return original_finish(
            self,
            backup_id,
            status,
            size_bytes=size_bytes,
            object_key=object_key,
            error=error,
            actor=actor,
            connection=connection,
        )

    monkeypatch.setattr(DatabaseBackupService, "finish", fail_success_persistence)
    handler = _handler(uow, cipher, FakeRuntime())

    handler._run_record(row.id, Actor.system("test"))

    assert prior in fake_s3.objects
    assert fake_s3.delete_calls == []
    assert fake_s3.completed_uploads
    assert DatabaseBackupService(uow).get(row.id).status == "failed"


def test_retention_prefix_does_not_cross_connection_name_prefixes(
    uow: UnitOfWork,
    cipher: SecretCipher,
    project: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_s3 = FakeS3()
    monkeypatch.setattr(
        "backend.worker.database_backup_handlers.boto3.client",
        lambda *_a, **_k: fake_s3,
    )
    main = _configured(uow, cipher, project, DatabaseEngine.POSTGRESQL)
    with uow:
        environment = uow.environments.get(main.environment_id)
        assert environment is not None
        second = uow.database_connections.add(
            DatabaseConnection(
                environment_id=environment.id,
                name="main2",
                engine=DatabaseEngine.POSTGRESQL,
                host=FIXTURE_HOST,
                port=5432,
                database=FIXTURE_SCHEMA,
                username=FIXTURE_LOGIN,
                password_secret=_connection_ref("main2"),
            )
        )
        second_id, environment_id = second.id, environment.id
    SecretService(uow, cipher).set(
        project,
        EnvironmentName.PRODUCTION,
        _connection_ref("main2"),
        DB_MARKER,
        Actor.system("test"),
    )
    second_backup = DatabaseBackupService(uow).create(second_id, environment_id, "manual")
    prior_main = f"backups/{project}/production/main/old.dump"
    prior_main2 = f"backups/{project}/production/main2/old.dump"
    fake_s3.objects.update({prior_main: b"main old", prior_main2: b"main2 old"})
    handler = _handler(uow, cipher, FakeRuntime())

    handler._run_record(main.id, Actor.system("test"))
    main_key = DatabaseBackupService(uow).get(main.id).object_key
    assert main_key in fake_s3.objects
    assert prior_main not in fake_s3.objects
    assert prior_main2 in fake_s3.objects

    handler._run_record(second_backup.id, Actor.system("test"))
    main2_key = DatabaseBackupService(uow).get(second_backup.id).object_key
    assert main_key in fake_s3.objects
    assert main2_key in fake_s3.objects
    assert prior_main2 not in fake_s3.objects


def test_dump_nonzero_exit_aborts_upload_and_preserves_previous(
    uow: UnitOfWork,
    cipher: SecretCipher,
    project: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_s3 = FakeS3()
    monkeypatch.setattr(
        "backend.worker.database_backup_handlers.boto3.client",
        lambda *_args, **_kwargs: fake_s3,
    )
    row = _configured(uow, cipher, project, DatabaseEngine.POSTGRESQL)
    prior = f"backups/{project}/production/main/2026-10-15.dump"
    same_day = object_key("backups", project, "production", "main", row.started_at, True)
    fake_s3.objects[prior] = b"old backup"
    fake_s3.objects[same_day] = b"previous same-day backup"
    runtime = FakeContainerRuntime()
    runtime.images.add("postgres:17")
    runtime.when("app", binary_chunks=(b"partial dump output",), exit_code=17)
    notifications: list[EventPayload] = []

    def notify(payload: EventPayload) -> int:
        notifications.append(payload)
        return 1

    handler = _handler(
        uow,
        cipher,
        runtime,
        notify=notify,
    )

    handler._run_record(row.id, Actor.system("test"))

    failed = DatabaseBackupService(uow).get(row.id)
    assert failed.status == "failed"
    assert prior in fake_s3.objects
    assert fake_s3.objects[same_day] == b"previous same-day backup"
    assert fake_s3.completed_uploads == []
    assert set(fake_s3.objects) == {prior, same_day}
    assert fake_s3.delete_calls == []
    assert len(notifications) == 1
    assert runtime.runs[0].env["PGPASSWORD"] == DB_MARKER
    assert DB_MARKER not in runtime.runs[0].command
    assert runtime.runs[0].network is not None
    assert runtime.runs[0].network.endswith("_default")
    assert not any(
        marker in record.getMessage()
        for record in caplog.records
        for marker in (DB_MARKER, S3_MARKER)
    )


def test_empty_object_verification_does_not_delete_previous_backup() -> None:
    client = FakeS3(empty_head=True)
    client.objects["backups/p/e/c/2026-10-16T030000.dump"] = b"new"
    client.objects["backups/p/e/c/2026-10-15T030000.dump"] = b"old"
    store = S3Store(cast(S3Client, client), "bucket")
    with pytest.raises(UpstreamError):
        verify_and_retain_latest(store, "backups/p/e/c/2026-10-16T030000.dump", "backups/p/e/c/")
    assert "backups/p/e/c/2026-10-15T030000.dump" in client.objects


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (BackupError("database password is not configured"), "database password is not configured"),
        (RuntimeError("private credential marker"), "database backup could not be completed"),
    ],
)
def test_failure_message_preserves_only_controlled_backup_errors(
    error: Exception, expected: str
) -> None:
    assert _safe_error(error) == expected
