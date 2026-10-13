"""TOTP primitives, the vault and the worker RPC around it."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from backend.adapters.worker_client import WorkerClient
from backend.domain.errors import WorkerError
from backend.services.secret_cipher import SecretCipher
from backend.services.totp import (
    TotpVault,
    decode_key,
    encode_key,
    hotp,
    matching_step,
    provisioning_uri,
    time_step,
)
from backend.worker.rpc import RpcServer
from backend.worker.totp_handlers import totp_handlers

RFC_KEY = b"12345678901234567890"


def test_hotp_matches_rfc4226_vectors() -> None:
    expected = ["755224", "287082", "359152", "969429", "338314", "254676"]
    assert [hotp(RFC_KEY, counter) for counter in range(6)] == expected


@pytest.mark.parametrize(
    ("unix_time", "code"),
    [(59, "287082"), (1111111109, "081804"), (1234567890, "005924"), (2000000000, "279037")],
)
def test_rfc6238_sha1_vectors(unix_time: int, code: str) -> None:
    assert hotp(RFC_KEY, time_step(unix_time)) == code
    assert matching_step(RFC_KEY, code, unix_time, None) == time_step(unix_time)


def test_window_allows_one_step_of_drift_but_not_two() -> None:
    now = 1_000_000.0
    code = hotp(RFC_KEY, time_step(now) - 1)
    assert matching_step(RFC_KEY, code, now, None) == time_step(now) - 1
    stale = hotp(RFC_KEY, time_step(now) - 2)
    assert matching_step(RFC_KEY, stale, now, None) is None


def test_a_step_is_accepted_once() -> None:
    now = 1_000_000.0
    code = hotp(RFC_KEY, time_step(now))
    step = matching_step(RFC_KEY, code, now, None)
    assert step is not None
    assert matching_step(RFC_KEY, code, now, step) is None


FULLWIDTH = "\uff11\uff12\uff13\uff14\uff15\uff16"


@pytest.mark.parametrize("code", ["", "12345", "1234567", "abcdef", "12 456", FULLWIDTH])
def test_malformed_codes_never_match(code: str) -> None:
    assert matching_step(RFC_KEY, code, 59.0, None) is None


def test_key_text_roundtrip_and_uri() -> None:
    text = encode_key(RFC_KEY)
    assert "=" not in text
    assert decode_key(text) == RFC_KEY
    uri = provisioning_uri("ada lovelace", text)
    assert uri.startswith("otpauth://totp/Jendock%3Aada%20lovelace?")
    assert f"secret={text}" in uri
    assert "issuer=Jendock" in uri


def test_vault_encrypts_and_checks(tmp_path: Path) -> None:
    now = 1_700_000_000.0
    vault = TotpVault(SecretCipher(tmp_path / "k"), clock=lambda: now)
    secret, ciphertext = vault.generate()
    assert secret.encode() not in ciphertext
    code = hotp(decode_key(secret), time_step(now))
    assert vault.check(ciphertext, code, None) == time_step(now)
    assert vault.check(ciphertext, code, time_step(now)) is None
    assert vault.check(ciphertext, "000000", None) is None


def test_vault_rejects_a_foreign_ciphertext(tmp_path: Path) -> None:
    cipher = SecretCipher(tmp_path / "k")
    vault = TotpVault(cipher)
    # A secret of another purpose encrypted with the same key must not act as a TOTP key.
    assert vault.check(cipher.encrypt("some project secret"), "123456", None) is None


@pytest.fixture
def socket_path() -> Iterator[Path]:
    directory = Path(tempfile.mkdtemp(prefix="tp"))
    yield directory / "w.sock"
    shutil.rmtree(directory, ignore_errors=True)


def test_worker_rpc_generates_and_checks(tmp_path: Path, socket_path: Path) -> None:
    now = 1_700_000_000.0
    vault = TotpVault(SecretCipher(tmp_path / "k"), clock=lambda: now)
    server = RpcServer(socket_path, totp_handlers(vault))
    server.start()
    try:
        client = WorkerClient(socket_path)
        enrollment = client.totp_generate()
        code = hotp(decode_key(enrollment.secret), time_step(now))
        assert client.totp_check(enrollment.ciphertext, code, None) == time_step(now)
        assert client.totp_check(enrollment.ciphertext, "000000", None) is None
        assert client.totp_check(enrollment.ciphertext, code, time_step(now)) is None
    finally:
        server.stop()


def test_worker_rpc_rejects_bad_params(tmp_path: Path, socket_path: Path) -> None:
    vault = TotpVault(SecretCipher(tmp_path / "k"))
    server = RpcServer(socket_path, totp_handlers(vault))
    server.start()
    try:
        client = WorkerClient(socket_path)
        with pytest.raises(WorkerError):
            client.totp_check(b"x" * 10_000, "123456", None)
    finally:
        server.stop()


def test_unreachable_worker_is_reported(tmp_path: Path) -> None:
    client = WorkerClient(tmp_path / "missing.sock")
    with pytest.raises(WorkerError):
        client.totp_generate()
