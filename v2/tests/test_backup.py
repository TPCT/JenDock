"""Backup create/restore, retention, the CLI and the daily schedule."""

from __future__ import annotations

import io
import json
import sqlite3
import stat
import tarfile
import threading
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from shiftcodes.backup.create import BackupSource, create_backup
from shiftcodes.backup.manifest import (
    BackupError,
    Manifest,
    archive_name,
    validate_member,
)
from shiftcodes.backup.restore import known_revisions, restore_backup
from shiftcodes.backup.retention import key_copy_of, list_archives, prune_archives
from shiftcodes.backup.schedule import BackupScheduler, next_run
from shiftcodes.cli import main
from shiftcodes.config import Settings, get_settings
from shiftcodes.domain.actor import Actor
from shiftcodes.repositories.database import (
    UnitOfWork,
    create_db_engine,
    create_session_factory,
)
from shiftcodes.repositories.migrate import upgrade_to_head
from shiftcodes.services.users import UserService
from tests.api_support import fast_hasher

KEY = b"super-secret-key-material"
NOW = datetime(2026, 10, 14, 3, 0, tzinfo=UTC)


class World:
    """A data directory with a migrated database, an override, logs and a key."""

    def __init__(self, root: Path) -> None:
        self.data = root / "data"
        self.data.mkdir()
        self.db = self.data / "shiftcodes.db"
        self.url = f"sqlite:///{self.db}"
        self.key = root / "keys" / "secret.key"
        self.key.parent.mkdir()
        self.key.write_bytes(KEY)
        self.key.chmod(0o600)
        upgrade_to_head(self.url)
        engine = create_db_engine(self.url)
        UserService(UnitOfWork(create_session_factory(engine)), fast_hasher()).create(
            "alice", "a-long-enough-password", Actor.system("test")
        )
        engine.dispose()
        override = self.data / "deployments" / "demo-dev"
        override.mkdir(parents=True)
        (override / "override.yml").write_text('{"services": {"web": {"image": "x:1"}}}\n')
        (override / "override.yml.prev").write_text('{"services": {}}\n')
        (override / "journal.json").write_text("{}")  # transient: never archived
        log = self.data / "logs" / "7"
        log.mkdir(parents=True)
        (log / "3.log").write_text("hello log\n")

    @property
    def source(self) -> BackupSource:
        return BackupSource(self.data, self.db, self.key)


@pytest.fixture
def world(tmp_path: Path) -> World:
    return World(tmp_path)


def _names(archive: Path) -> list[str]:
    with tarfile.open(archive) as tar:
        return tar.getnames()


def _rewrite(archive: Path, target: Path, edit: dict[str, bytes | None]) -> None:
    """Copy ``archive`` replacing (or dropping, with ``None``) members, keeping the manifest."""
    with tarfile.open(archive) as source, tarfile.open(target, "w:gz") as out:
        for member in source:
            handle = source.extractfile(member)
            data = handle.read() if handle else b""
            if member.name in edit:
                replacement = edit[member.name]
                if replacement is None:
                    continue
                data = replacement
            info = tarfile.TarInfo(member.name)
            info.size = len(data)
            out.addfile(info, io.BytesIO(data))
        names = set(source.getnames())
        for name, added in edit.items():
            if name not in names and added is not None:
                info = tarfile.TarInfo(name)
                info.size = len(added)
                out.addfile(info, io.BytesIO(added))


# --- create ----------------------------------------------------------------------------------


def test_archive_holds_snapshot_overrides_and_manifest_without_the_key(
    world: World, tmp_path: Path
) -> None:
    result = create_backup(world.source, tmp_path / "out", now=NOW)
    assert result.archive.name == "shiftcodes-backup-20261014T030000Z.tar.gz"
    names = _names(result.archive)
    assert sorted(names) == [
        "db/shiftcodes.db",
        "manifest.json",
        "overrides/demo-dev/override.yml",
        "overrides/demo-dev/override.yml.prev",
    ]
    assert stat.S_IMODE(result.archive.stat().st_mode) == 0o600
    assert result.key_copy is None
    assert KEY not in result.archive.read_bytes()
    _head = known_revisions()[1]
    assert result.manifest.alembic_revision == _head
    with tarfile.open(result.archive) as tar:
        handle = tar.extractfile("manifest.json")
        assert handle is not None
        data = json.loads(handle.read())
    assert data["includes_secret_key"] is False
    assert data["includes_logs"] is False
    assert all(len(f["sha256"]) == 64 for f in data["files"])


def test_logs_are_optional(world: World, tmp_path: Path) -> None:
    result = create_backup(world.source, tmp_path / "out", include_logs=True, now=NOW)
    assert "logs/7/3.log" in _names(result.archive)
    assert result.manifest.includes_logs


def test_include_key_writes_a_separate_private_file(world: World, tmp_path: Path) -> None:
    result = create_backup(world.source, tmp_path / "out", include_key=True, now=NOW)
    assert result.key_copy is not None
    assert result.key_copy == key_copy_of(result.archive)
    assert result.key_copy.read_bytes() == KEY
    assert stat.S_IMODE(result.key_copy.stat().st_mode) == 0o600
    assert KEY not in result.archive.read_bytes()
    assert not any("key" in n for n in _names(result.archive))


def test_snapshot_is_consistent_while_another_connection_writes(
    world: World, tmp_path: Path
) -> None:
    writer = sqlite3.connect(world.db)
    writer.execute("BEGIN")
    writer.execute("UPDATE users SET username = 'uncommitted'")
    result = create_backup(world.source, tmp_path / "out", now=NOW)
    writer.rollback()
    writer.close()
    restored = tmp_path / "restored"
    restore_backup(result.archive, BackupSource(restored, restored / "db.sqlite", tmp_path / "k"))
    with closing(sqlite3.connect(restored / "db.sqlite")) as database:
        assert database.execute("SELECT username FROM users").fetchall() == [("alice",)]


def test_create_refuses_a_missing_or_unmigrated_database(tmp_path: Path) -> None:
    empty = tmp_path / "data"
    empty.mkdir()
    source = BackupSource(empty, empty / "none.db", tmp_path / "k")
    with pytest.raises(BackupError, match="database not found"):
        create_backup(source, tmp_path / "out", now=NOW)
    sqlite3.connect(empty / "plain.db").close()
    source = BackupSource(empty, empty / "plain.db", tmp_path / "k")
    with pytest.raises(BackupError, match="not been migrated"):
        create_backup(source, tmp_path / "out2", now=NOW)
    assert list((tmp_path / "out2").iterdir()) == []


def test_two_backups_in_the_same_second_do_not_overwrite(world: World, tmp_path: Path) -> None:
    create_backup(world.source, tmp_path / "out", now=NOW)
    with pytest.raises(BackupError, match="already exists"):
        create_backup(world.source, tmp_path / "out", now=NOW)


# --- restore ---------------------------------------------------------------------------------


def test_round_trip_restores_everything_and_migrations_still_pass(
    world: World, tmp_path: Path
) -> None:
    result = create_backup(world.source, tmp_path / "out", include_logs=True, now=NOW)
    target = tmp_path / "new" / "data"
    new = BackupSource(target, target / "shiftcodes.db", tmp_path / "new" / "keys" / "secret.key")
    restored = restore_backup(result.archive, new, key_file=None)
    assert restored.files == 4
    assert not restored.key_restored
    assert (target / "deployments" / "demo-dev" / "override.yml").read_text().startswith('{"serv')
    assert (target / "logs" / "7" / "3.log").read_text() == "hello log\n"
    assert not (target / "deployments" / "demo-dev" / "journal.json").exists()
    assert stat.S_IMODE((target / "shiftcodes.db").stat().st_mode) == 0o660
    url = f"sqlite:///{target / 'shiftcodes.db'}"
    upgrade_to_head(url)  # `make migrate` on the restored database
    engine = create_db_engine(url)
    with UnitOfWork(create_session_factory(engine)) as uow:
        assert uow.users.get_by_username("alice") is not None
    engine.dispose()
    with closing(sqlite3.connect(target / "shiftcodes.db")) as database:
        assert database.execute("SELECT version_num FROM alembic_version").fetchone() == (
            known_revisions()[1],
        )


def test_restore_can_put_a_saved_key_in_place(world: World, tmp_path: Path) -> None:
    result = create_backup(world.source, tmp_path / "out", include_key=True, now=NOW)
    target = tmp_path / "new"
    new = BackupSource(target, target / "shiftcodes.db", tmp_path / "keys2" / "secret.key")
    assert result.key_copy is not None
    restored = restore_backup(result.archive, new, key_file=result.key_copy)
    assert restored.key_restored
    assert new.key_file.read_bytes() == KEY
    assert stat.S_IMODE(new.key_file.stat().st_mode) == 0o600


def test_restore_refuses_a_non_empty_data_dir_or_existing_database(
    world: World, tmp_path: Path
) -> None:
    archive = create_backup(world.source, tmp_path / "out", now=NOW).archive
    with pytest.raises(BackupError, match="not empty"):
        restore_backup(archive, world.source)
    elsewhere = tmp_path / "empty"
    elsewhere.mkdir()
    with pytest.raises(BackupError, match="already exists"):
        restore_backup(archive, BackupSource(elsewhere, world.db, tmp_path / "k"))
    assert list(elsewhere.iterdir()) == []


def test_restore_detects_a_tampered_member(world: World, tmp_path: Path) -> None:
    archive = create_backup(world.source, tmp_path / "out", now=NOW).archive
    forged = tmp_path / "forged.tar.gz"
    _rewrite(archive, forged, {"overrides/demo-dev/override.yml": b"changed"})
    target = tmp_path / "t"
    with pytest.raises(BackupError, match="checksum mismatch"):
        restore_backup(forged, BackupSource(target, target / "d.db", tmp_path / "k"))
    assert not target.exists() or list(target.iterdir()) == []


def test_restore_detects_missing_and_extra_members(world: World, tmp_path: Path) -> None:
    archive = create_backup(world.source, tmp_path / "out", now=NOW).archive
    target = tmp_path / "t"
    new = BackupSource(target, target / "d.db", tmp_path / "k")
    missing = tmp_path / "missing.tar.gz"
    _rewrite(archive, missing, {"overrides/demo-dev/override.yml.prev": None})
    with pytest.raises(BackupError, match="does not match its manifest"):
        restore_backup(missing, new)
    extra = tmp_path / "extra.tar.gz"
    _rewrite(archive, extra, {"logs/injected": b"x"})
    with pytest.raises(BackupError, match="does not match its manifest"):
        restore_backup(extra, new)


@pytest.mark.parametrize("name", ["../evil", "/etc/passwd", "overrides/../../x", "other/file"])
def test_unsafe_or_unexpected_member_names_are_rejected(name: str) -> None:
    with pytest.raises(BackupError):
        validate_member(name)


def test_restore_refuses_a_backup_from_a_newer_revision(world: World, tmp_path: Path) -> None:
    archive = create_backup(world.source, tmp_path / "out", now=NOW).archive
    with tarfile.open(archive) as tar:
        handle = tar.extractfile("manifest.json")
        assert handle is not None
        manifest = json.loads(handle.read())
    manifest["alembic_revision"] = "9999_future"
    forged = tmp_path / "future.tar.gz"
    _rewrite(archive, forged, {"manifest.json": json.dumps(manifest).encode()})
    target = tmp_path / "t"
    with pytest.raises(BackupError, match="unknown to this version"):
        restore_backup(forged, BackupSource(target, target / "d.db", tmp_path / "k"))
    assert not target.exists() or list(target.iterdir()) == []


def test_restore_cleans_up_when_the_database_does_not_match_its_revision(
    world: World, tmp_path: Path
) -> None:
    archive = create_backup(world.source, tmp_path / "out", now=NOW).archive
    manifest_member = "manifest.json"
    with tarfile.open(archive) as tar:
        handle = tar.extractfile(manifest_member)
        assert handle is not None
        manifest = json.loads(handle.read())
    manifest["alembic_revision"] = "0001"
    forged = tmp_path / "wrongrev.tar.gz"
    _rewrite(archive, forged, {manifest_member: json.dumps(manifest).encode()})
    target = tmp_path / "t"
    with pytest.raises(BackupError, match="does not match the revision"):
        restore_backup(forged, BackupSource(target, target / "d.db", tmp_path / "k"))
    assert list(target.rglob("*")) == []


def test_manifest_rejects_other_formats() -> None:
    with pytest.raises(BackupError, match="unsupported"):
        Manifest.from_json(
            '{"format_version": 99, "created_at": "x", "alembic_revision": "1",'
            ' "includes_logs": false, "files": []}'
        )
    with pytest.raises(BackupError, match="malformed"):
        Manifest.from_json("{}")


# --- retention and schedule ------------------------------------------------------------------


def test_prune_keeps_the_newest_and_only_touches_archives(tmp_path: Path) -> None:
    for day in range(1, 6):
        stamp = datetime(2026, 10, day, 3, 0, tzinfo=UTC)
        (tmp_path / archive_name(stamp)).write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("keep me")
    (tmp_path / "shiftcodes-backup-20261001T030000Z.secret.key").write_text("k")
    removed = prune_archives(tmp_path, 2)
    assert [p.name[18:26] for p in removed] == ["20261003", "20261002", "20261001"]
    assert [p.name[18:26] for p in list_archives(tmp_path)] == ["20261005", "20261004"]
    assert (tmp_path / "notes.txt").exists()
    assert (tmp_path / "shiftcodes-backup-20261001T030000Z.secret.key").exists()
    with pytest.raises(ValueError, match="at least 1"):
        prune_archives(tmp_path, 0)


def test_next_run_is_the_next_configured_hour() -> None:
    assert next_run(datetime(2026, 10, 14, 1, 0, tzinfo=UTC), 3) == datetime(
        2026, 10, 14, 3, 0, tzinfo=UTC
    )
    assert next_run(datetime(2026, 10, 14, 3, 0, tzinfo=UTC), 3) == datetime(
        2026, 10, 15, 3, 0, tzinfo=UTC
    )
    assert next_run(datetime(2026, 10, 14, 23, 59, tzinfo=UTC), 0) == datetime(
        2026, 10, 15, 0, 0, tzinfo=UTC
    )


def _settings(world: World, backups: Path, **extra: object) -> Settings:
    return Settings.model_validate(
        {
            "data_dir": world.data,
            "database_url": world.url,
            "secret_key_file": world.key,
            "backup_dir": backups,
            **extra,
        }
    )


def test_backups_are_off_by_default(world: World) -> None:
    settings = _settings(world, world.data.parent / "b")
    assert settings.backup_enabled is False
    assert (settings.backup_keep, settings.backup_hour_utc) == (7, 3)


def test_scheduler_backs_up_prunes_and_never_includes_the_key(world: World, tmp_path: Path) -> None:
    backups = tmp_path / "scheduled"
    settings = _settings(world, backups, backup_keep=2, backup_enabled=True)
    moments = iter(datetime(2026, 10, d, 3, 0, tzinfo=UTC) for d in range(1, 5))
    scheduler = BackupScheduler(settings, clock=lambda: next(moments))
    for _ in range(4):
        scheduler.run_once()
    assert [p.name[18:26] for p in list_archives(backups)] == ["20261004", "20261003"]
    assert not list(backups.glob("*.secret.key"))


def test_scheduler_loop_waits_then_runs_until_shutdown(world: World, tmp_path: Path) -> None:
    backups = tmp_path / "loop"
    settings = _settings(world, backups)
    clock_values = [datetime(2026, 10, 14, 1, 0, tzinfo=UTC)] * 2

    class Stop:
        def __init__(self) -> None:
            self.waits: list[float] = []

        def wait(self, timeout: float) -> bool:
            self.waits.append(timeout)
            return len(self.waits) > 1

    stop = Stop()
    times = iter([*clock_values, datetime(2026, 10, 14, 3, 0, tzinfo=UTC)] * 3)
    scheduler = BackupScheduler(settings, clock=lambda: next(times))
    scheduler.run_forever(stop)
    assert stop.waits[0] == 2 * 3600
    assert len(list_archives(backups)) == 1


def test_scheduler_requires_a_directory(world: World) -> None:
    settings = Settings.model_validate(
        {"data_dir": world.data, "database_url": world.url, "secret_key_file": world.key}
    )
    with pytest.raises(BackupError, match="BACKUP_DIR"):
        BackupScheduler(settings)


def test_failed_scheduled_backup_is_logged_not_raised(world: World, tmp_path: Path) -> None:
    settings = _settings(world, tmp_path / "x")
    world.db.unlink()
    for suffix in ("-wal", "-shm"):
        Path(str(world.db) + suffix).unlink(missing_ok=True)

    class OneShot:
        def __init__(self) -> None:
            self.calls = 0

        def wait(self, timeout: float) -> bool:
            self.calls += 1
            return self.calls > 1

    BackupScheduler(settings).run_forever(OneShot())  # must not raise


# --- command line ----------------------------------------------------------------------------


def test_cli_create_restore_prune(
    world: World,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("SHIFTCODES_DATA_DIR", str(world.data))
    monkeypatch.setenv("SHIFTCODES_DATABASE_URL", world.url)
    monkeypatch.setenv("SHIFTCODES_SECRET_KEY_FILE", str(world.key))
    get_settings.cache_clear()
    out = tmp_path / "cli-out"
    assert main(["backup", "create", "--to", str(out), "--include-key", "--keep", "5"]) == 0
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "secret.key" in captured.err
    archive = list_archives(out)[0]
    # restore into a fresh location
    fresh = tmp_path / "fresh"
    monkeypatch.setenv("SHIFTCODES_DATA_DIR", str(fresh))
    monkeypatch.setenv("SHIFTCODES_DATABASE_URL", f"sqlite:///{fresh / 'shiftcodes.db'}")
    monkeypatch.setenv("SHIFTCODES_SECRET_KEY_FILE", str(fresh / "secret.key"))
    get_settings.cache_clear()
    assert (
        main(["backup", "restore", "--from", str(archive), "--key-file", str(key_copy_of(archive))])
        == 0
    )
    assert (fresh / "shiftcodes.db").exists()
    assert main(["backup", "restore", "--from", str(archive)]) == 1  # not empty any more
    assert "refusing to restore" in capsys.readouterr().err
    assert main(["backup", "prune", "--dir", str(out), "--keep", "1"]) == 0
    get_settings.cache_clear()


def test_scheduler_thread_stops_with_shutdown(world: World, tmp_path: Path) -> None:
    settings = _settings(world, tmp_path / "t")
    stop = threading.Event()

    class Waiter:
        def wait(self, timeout: float) -> bool:
            return stop.wait(0.01)

    stop.set()
    thread = threading.Thread(target=BackupScheduler(settings).run_forever, args=(Waiter(),))
    thread.start()
    thread.join(5)
    assert not thread.is_alive()
