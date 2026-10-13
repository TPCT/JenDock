import logging
import os
import stat
from pathlib import Path

import pytest

from backend.domain.actor import Actor
from backend.domain.enums import ActorType
from backend.domain.errors import ConfigurationError, NotFoundError, ValidationError
from backend.logging import REDACTED, SecretRedactor
from backend.repositories.database import UnitOfWork
from backend.services.audit import AuditService
from backend.services.environments import EnvironmentService
from backend.services.secret_cipher import SecretCipher
from backend.services.secrets import SecretService
from tests.conftest import DEV, PROD

VALUE = "s3cr3t-value-XYZ"
USER = Actor(ActorType.USER, "7")


def _service(
    uow: UnitOfWork, cipher: SecretCipher, redactor: SecretRedactor | None = None
) -> SecretService:
    return SecretService(uow, cipher, redactor)


def _audit_dump(uow: UnitOfWork) -> str:
    return repr([(e.action, e.actor_type, e.actor_id, e.details) for e in AuditService(uow).list()])


def test_cipher_roundtrip_and_ciphertext_differs(cipher: SecretCipher) -> None:
    encrypted = cipher.encrypt(VALUE)
    assert VALUE.encode() not in encrypted
    assert cipher.decrypt(encrypted) == VALUE


def test_cipher_key_file_created_0600_and_reused(tmp_path: Path) -> None:
    key_file = tmp_path / "a" / "secret.key"
    first = SecretCipher(key_file)
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
    assert SecretCipher(key_file).decrypt(first.encrypt("x")) == "x"


@pytest.mark.parametrize("mode", [0o640, 0o604, 0o660, 0o644, 0o700 | 0o010])
def test_cipher_refuses_group_or_world_access(tmp_path: Path, mode: int) -> None:
    key_file = tmp_path / "secret.key"
    SecretCipher(key_file)
    key_file.chmod(mode)
    with pytest.raises(ConfigurationError, match="group or others"):
        SecretCipher(key_file)


def test_cipher_refuses_garbage_key_and_wrong_key(tmp_path: Path) -> None:
    bad = tmp_path / "bad.key"
    bad.write_text("not a key")
    bad.chmod(0o600)
    with pytest.raises(ConfigurationError):
        SecretCipher(bad)
    token = SecretCipher(tmp_path / "one.key").encrypt("x")
    with pytest.raises(ConfigurationError):
        SecretCipher(tmp_path / "two.key").decrypt(token)


def test_set_list_and_never_exposes_value(
    uow: UnitOfWork, cipher: SecretCipher, project: str
) -> None:
    service = _service(uow, cipher)
    service.set(project, DEV, "DB_PASSWORD", VALUE, USER)
    service.set(project, DEV, "API", "other", USER)
    infos = service.list_names(project, DEV)
    assert [i.name for i in infos] == ["API", "DB_PASSWORD"]
    assert VALUE not in repr(infos)
    assert service.list_names(project, PROD) == []
    env = EnvironmentService(uow).get(project, DEV)
    with uow:
        stored = uow.secrets.get(env.id, "DB_PASSWORD")
        assert stored is not None
        assert VALUE.encode() not in stored.ciphertext


def test_rotate_keeps_creation_time(uow: UnitOfWork, cipher: SecretCipher, project: str) -> None:
    service = _service(uow, cipher)
    first = service.set(project, DEV, "TOKEN_A", "v1", USER)
    second = service.set(project, DEV, "TOKEN_A", "v2", USER)
    assert second.created_at == first.created_at
    assert second.updated_at >= first.updated_at
    assert len(service.list_names(project, DEV)) == 1
    assert service.reveal(project, DEV, "TOKEN_A", USER) == "v2"
    actions = [e.action for e in AuditService(uow).list()]
    assert actions == ["secret.reveal", "secret.rotate", "secret.create"]


def test_reveal_audits_without_value(uow: UnitOfWork, cipher: SecretCipher, project: str) -> None:
    service = _service(uow, cipher)
    service.set(project, DEV, "DB_PASSWORD", VALUE, USER)
    assert service.reveal(project, DEV, "DB_PASSWORD", USER) == VALUE
    events = AuditService(uow).list(action="secret.reveal")
    assert len(events) == 1
    assert events[0].actor_id == "7"
    assert events[0].details == {"name": "DB_PASSWORD"}
    assert VALUE not in _audit_dump(uow)


def test_resolve_for_run_audits_as_system(
    uow: UnitOfWork, cipher: SecretCipher, project: str
) -> None:
    service = _service(uow, cipher)
    service.set(project, DEV, "A_ONE", VALUE, USER)
    service.set(project, DEV, "B_TWO", "second", USER)
    env = EnvironmentService(uow).get(project, DEV)
    values = service.resolve_for_run(env.id, ["B_TWO", "A_ONE", "A_ONE"], run_id=5)
    assert values == {"A_ONE": VALUE, "B_TWO": "second"}
    event = AuditService(uow).list(action="secret.resolve")[0]
    assert event.actor_type is ActorType.SYSTEM
    assert event.details == {"names": ["A_ONE", "B_TWO"], "run_id": 5}
    assert VALUE not in _audit_dump(uow)


def test_resolve_missing_and_unknown_environment(
    uow: UnitOfWork, cipher: SecretCipher, project: str
) -> None:
    service = _service(uow, cipher)
    env = EnvironmentService(uow).get(project, DEV)
    with pytest.raises(NotFoundError, match="NOPE"):
        service.resolve_for_run(env.id, ["NOPE"])
    with pytest.raises(NotFoundError):
        service.resolve_for_run(9999, ["NOPE"])
    assert AuditService(uow).list(action="secret.resolve") == []


def test_delete_and_missing(uow: UnitOfWork, cipher: SecretCipher, project: str) -> None:
    service = _service(uow, cipher)
    service.set(project, DEV, "GONE", VALUE, USER)
    service.delete(project, DEV, "GONE", USER)
    assert service.list_names(project, DEV) == []
    with pytest.raises(NotFoundError):
        service.delete(project, DEV, "GONE", USER)
    with pytest.raises(NotFoundError):
        service.reveal(project, DEV, "GONE", USER)


def test_validation(uow: UnitOfWork, cipher: SecretCipher, project: str) -> None:
    service = _service(uow, cipher)
    with pytest.raises(ValidationError):
        service.set(project, DEV, "lower", VALUE, USER)
    with pytest.raises(ValidationError):
        service.set(project, DEV, "OK", "", USER)
    with pytest.raises(ValidationError):
        service.set(project, DEV, "OK", "x" * (64 * 1024 + 1), USER)


def test_revealed_values_registered_with_redactor(
    uow: UnitOfWork, cipher: SecretCipher, project: str
) -> None:
    redactor = SecretRedactor()
    service = _service(uow, cipher, redactor)
    service.set(project, DEV, "K_ONE", VALUE, USER)
    assert redactor.redact_text(f"v={VALUE}") == f"v={REDACTED}"
    fresh = SecretRedactor()
    _service(uow, cipher, fresh).reveal(project, DEV, "K_ONE", USER)
    assert fresh.redact_text(VALUE) == REDACTED
    env = EnvironmentService(uow).get(project, DEV)
    third = SecretRedactor()
    _service(uow, cipher, third).resolve_for_run(env.id, ["K_ONE"])
    assert third.redact_text(VALUE) == REDACTED


def test_value_never_logged(
    uow: UnitOfWork, cipher: SecretCipher, project: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    service = _service(uow, cipher)
    service.set(project, DEV, "K_ONE", VALUE, USER)
    service.reveal(project, DEV, "K_ONE", USER)
    assert VALUE not in caplog.text


def test_key_file_mode_from_umask_does_not_matter(tmp_path: Path) -> None:
    old = os.umask(0)
    try:
        key_file = tmp_path / "k"
        SecretCipher(key_file)
    finally:
        os.umask(old)
    assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
