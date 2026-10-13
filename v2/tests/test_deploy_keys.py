"""Per-repository deploy keys: generation, storage, the CLI and its audit trail."""

from __future__ import annotations

import argparse
import shutil
import stat
from pathlib import Path

import pytest

from backend.adapters.deploy_keys import DeployKeyStore
from backend.adapters.git_access import deploy_key_path
from backend.cli import build_parser
from backend.deploy_keys_cli import audit_recorder, run_deploy_key
from backend.domain.errors import ConfigurationError, ConflictError, NotFoundError, ValidationError
from backend.repositories.database import UnitOfWork
from backend.services.audit import AuditService

needs_keygen = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="no ssh-keygen")


@pytest.fixture
def store(tmp_path: Path) -> DeployKeyStore:
    return DeployKeyStore(tmp_path / "keys" / "deploy_keys")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _args(*argv: str) -> argparse.Namespace:
    return build_parser().parse_args(["deploy-key", *argv])


@needs_keygen
def test_create_stores_a_private_key_and_returns_only_the_public_line(
    store: DeployKeyStore, tmp_path: Path
) -> None:
    created = store.create("octo/demo")
    directory = tmp_path / "keys" / "deploy_keys"
    key = directory / "octo__demo"
    assert created.rotated is False
    assert created.public_key.startswith("ssh-ed25519 ")
    assert created.public_key.endswith(" jendock:octo/demo")
    assert "PRIVATE KEY" not in created.public_key
    assert "PRIVATE KEY" in key.read_text()
    assert _mode(key) == 0o600
    assert _mode(directory) == 0o700
    assert [p.name for p in directory.iterdir()] == ["octo__demo"]  # scratch files are gone


@needs_keygen
def test_create_refuses_to_overwrite_unless_rotating(store: DeployKeyStore, tmp_path: Path) -> None:
    first = store.create("octo/demo")
    key = tmp_path / "keys" / "deploy_keys" / "octo__demo"
    before = key.read_text()
    with pytest.raises(ConflictError, match="--rotate"):
        store.create("octo/demo")
    assert key.read_text() == before
    second = store.create("octo/demo", rotate=True)
    assert second.rotated is True
    assert second.public_key != first.public_key
    assert key.read_text() != before
    assert _mode(key) == 0o600


@needs_keygen
def test_show_derives_the_public_key_and_list_names_the_repositories(
    store: DeployKeyStore,
) -> None:
    created = store.create("octo/demo")
    store.create("acme/Other.Repo")
    assert store.public_key("octo/demo") == created.public_key
    assert store.repositories() == ["acme/Other.Repo", "octo/demo"]
    with pytest.raises(NotFoundError):
        store.public_key("octo/missing")


def test_list_of_a_missing_directory_is_empty(tmp_path: Path) -> None:
    assert DeployKeyStore(tmp_path / "nope").repositories() == []


def test_names_are_validated_and_cannot_traverse(store: DeployKeyStore, tmp_path: Path) -> None:
    for bad in ("../x/y", "octo/..", "octo", "a/b/c", "octo/demo.git", "/abs/path"):
        with pytest.raises(ValidationError):
            store.create(bad)
        with pytest.raises(ValidationError):
            store.public_key(bad)
        with pytest.raises(ValidationError):
            deploy_key_path(tmp_path, bad)
    assert not (tmp_path / "keys").exists()


def test_a_missing_ssh_keygen_is_a_clear_error(tmp_path: Path) -> None:
    broken = DeployKeyStore(tmp_path / "keys", keygen=str(tmp_path / "no-ssh-keygen"))
    with pytest.raises(ConfigurationError, match="could not be run"):
        broken.create("octo/demo")
    assert list((tmp_path / "keys").iterdir()) == []


@needs_keygen
def test_the_cli_prints_only_the_public_key_and_audits_without_key_material(
    store: DeployKeyStore,
    uow: UnitOfWork,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    record = audit_recorder(AuditService(uow))
    assert run_deploy_key(_args("create", "octo/demo"), store, record) == 0
    out = capsys.readouterr()
    assert out.out.count("\n") == 1 and out.out.startswith("ssh-ed25519 ")
    assert out.err == ""
    private = (tmp_path / "keys" / "deploy_keys" / "octo__demo").read_text()
    assert private.strip().splitlines()[1] not in out.out + out.err

    assert run_deploy_key(_args("create", "octo/demo"), store, record) == 1
    assert "--rotate" in capsys.readouterr().err
    assert run_deploy_key(_args("create", "octo/demo", "--rotate"), store, record) == 0
    capsys.readouterr()

    events = AuditService(uow).list()
    assert [e.action for e in events] == ["deploy_key.rotate", "deploy_key.create"]
    assert {(e.target_type, e.target_id) for e in events} == {("repository", "octo/demo")}
    assert all(e.details == {"repository": "octo/demo"} for e in events)


@needs_keygen
def test_the_cli_shows_and_lists(store: DeployKeyStore, capsys: pytest.CaptureFixture[str]) -> None:
    run_deploy_key(_args("create", "octo/demo"), store)
    public = capsys.readouterr().out
    assert run_deploy_key(_args("show", "octo/demo"), store) == 0
    assert capsys.readouterr().out == public
    assert run_deploy_key(_args("list"), store) == 0
    assert capsys.readouterr().out == "octo/demo\n"
    assert run_deploy_key(_args("show", "octo/none"), store) == 1
    assert "no deploy key" in capsys.readouterr().err
