import io
import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from backend.cli import CLI_ACTOR, build_parser, main, run
from backend.config import get_settings
from backend.domain.errors import ValidationError
from backend.repositories.database import UnitOfWork
from backend.repositories.migrate import upgrade_to_head
from backend.services.audit import AuditService
from backend.services.users import UserService
from tests.api_support import fast_hasher

PASSWORD = "a-long-enough-password"


@pytest.fixture
def service(uow: UnitOfWork) -> UserService:
    return UserService(uow, fast_hasher())


def _prompt(*answers: str) -> Callable[[str], str]:
    values = iter(answers)
    return lambda _text: next(values)


def test_password_argument_is_not_accepted() -> None:
    parser = build_parser()
    for flag in ("--password", "--pass", "-p"):
        with pytest.raises(SystemExit) as exit_info:
            parser.parse_args(["users", "create", "alice", flag, PASSWORD])
        assert exit_info.value.code == 2


def test_create_reads_password_from_stdin(service: UserService, uow: UnitOfWork) -> None:
    code = run(
        ["users", "create", "alice", "--password-stdin"], service, io.StringIO(PASSWORD + "\n")
    )
    assert code == 0
    with uow:
        user = uow.users.get_by_username("alice")
        assert user is not None
        assert user.password_hash.startswith("$argon2id$")
        assert PASSWORD not in user.password_hash


def test_create_prompts_twice_without_stdin(service: UserService, uow: UnitOfWork) -> None:
    assert run(["users", "create", "alice"], service, prompt=_prompt(PASSWORD, PASSWORD)) == 0
    with uow:
        assert uow.users.get_by_username("alice") is not None


def test_prompt_mismatch_fails(
    service: UserService, uow: UnitOfWork, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        run(["users", "create", "alice"], service, prompt=_prompt(PASSWORD, "different-one-1")) == 1
    )
    assert "do not match" in capsys.readouterr().err
    with uow:
        assert uow.users.get_by_username("alice") is None


def test_short_password_rejected(service: UserService, capsys: pytest.CaptureFixture[str]) -> None:
    code = run(["users", "create", "alice", "--password-stdin"], service, io.StringIO("short\n"))
    assert code == 1
    captured = capsys.readouterr()
    assert "12" in captured.err
    assert "short" not in captured.err.replace("shortcodes", "")


def test_environment_variables_are_not_a_password_source(
    service: UserService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PASSWORD", PASSWORD)
    monkeypatch.setenv("JENDOCK_PASSWORD", PASSWORD)
    assert run(["users", "create", "alice"], service, prompt=_prompt("x" * 12, "y" * 12)) == 1


def test_duplicate_user_reports_error(
    service: UserService, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["users", "create", "alice", "--password-stdin"]
    assert run(args, service, io.StringIO(PASSWORD)) == 0
    assert run(args, service, io.StringIO(PASSWORD)) == 1
    assert "already exists" in capsys.readouterr().err


def test_reset_password_changes_hash_and_audits(service: UserService, uow: UnitOfWork) -> None:
    run(["users", "create", "alice", "--password-stdin"], service, io.StringIO(PASSWORD))
    with uow:
        before = uow.users.get_by_username("alice")
        assert before is not None
        old_hash = before.password_hash
    new_password = "another-long-password"
    assert (
        run(
            ["users", "reset-password", "alice", "--password-stdin"],
            service,
            io.StringIO(new_password),
        )
        == 0
    )
    with uow:
        after = uow.users.get_by_username("alice")
        assert after is not None
        assert after.password_hash != old_hash
    actions = [e.action for e in AuditService(uow).list()]
    assert actions == ["user.reset_password", "user.create"]
    dump = repr([(e.details, e.actor_id) for e in AuditService(uow).list()])
    assert PASSWORD not in dump
    assert new_password not in dump


def test_reset_unknown_user_fails(service: UserService, capsys: pytest.CaptureFixture[str]) -> None:
    code = run(
        ["users", "reset-password", "ghost", "--password-stdin"], service, io.StringIO(PASSWORD)
    )
    assert code == 1
    assert "not found" in capsys.readouterr().err


def test_username_validation(service: UserService) -> None:
    with pytest.raises(ValidationError):
        service.create("Bad Name", PASSWORD, CLI_ACTOR)


@pytest.fixture
def restore_umask() -> Iterator[None]:
    previous = os.umask(0o022)
    os.umask(previous)
    yield
    os.umask(previous)


@pytest.mark.usefixtures("restore_umask")
def test_main_uses_database_from_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("JENDOCK_DATABASE_URL", f"sqlite:///{tmp_path / 'cli.db'}")
    get_settings.cache_clear()
    upgrade_to_head()
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert main(["users", "create", "alice", "--password-stdin"]) == 0
    assert "created user alice" in capsys.readouterr().out
    get_settings.cache_clear()


def test_disable_two_factor_clears_the_second_factor(
    service: UserService, uow: UnitOfWork, capsys: pytest.CaptureFixture[str]
) -> None:
    run(["users", "create", "alice", "--password-stdin"], service, io.StringIO(PASSWORD))
    with uow:
        user = uow.users.get_by_username("alice")
        assert user is not None
        user.totp_secret = b"ciphertext"
        user.totp_enabled_at = user.created_at
        uow.users.replace_recovery_codes(user.id, ["a" * 64], user.created_at)
        user_id = user.id
    assert run(["users", "disable-two-factor", "alice"], service) == 0
    assert "turned off for alice" in capsys.readouterr().out
    with uow:
        after = uow.users.get_by_username("alice")
        assert after is not None
        assert after.totp_secret is None
        assert after.totp_enabled_at is None
        assert uow.users.count_recovery_codes(user_id) == 0
    assert AuditService(uow).list()[0].action == "two_factor.disable"


def test_disable_two_factor_unknown_user_fails(
    service: UserService, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run(["users", "disable-two-factor", "ghost"], service) == 1
    assert "not found" in capsys.readouterr().err
