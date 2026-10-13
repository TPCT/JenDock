"""Encryption of secret values at rest (Fernet) with a protected key file."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from backend.domain.errors import ConfigurationError

KEY_FILE_MODE = 0o600
_FORBIDDEN_MODE_BITS = stat.S_IRWXG | stat.S_IRWXO


class SecretCipher:
    """Encrypts and decrypts secret values with a key stored in a 0600 file.

    The key file is created (mode 0600) if missing. An existing file readable or
    writable by group or others is refused, because the key protects every secret.
    """

    def __init__(self, key_file: Path) -> None:
        self._fernet = Fernet(_load_or_create_key(key_file))

    def encrypt(self, plaintext: str) -> bytes:
        """Return the ciphertext for ``plaintext``."""
        return self._fernet.encrypt(plaintext.encode())

    def decrypt(self, ciphertext: bytes) -> str:
        """Return the plaintext; ``ConfigurationError`` if the key does not match."""
        try:
            return self._fernet.decrypt(ciphertext).decode()
        except InvalidToken as error:
            raise ConfigurationError("secret cannot be decrypted with the current key") from error


def _load_or_create_key(key_file: Path) -> bytes:
    if not key_file.exists():
        _create_key_file(key_file)
    _require_private(key_file)
    key = key_file.read_bytes().strip()
    try:
        Fernet(key)
    except ValueError as error:
        raise ConfigurationError(f"secret key file {key_file} does not hold a valid key") from error
    return key


def _create_key_file(key_file: Path) -> None:
    key_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, KEY_FILE_MODE)
    except FileExistsError:
        return
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(Fernet.generate_key())


def _require_private(key_file: Path) -> None:
    mode = key_file.stat().st_mode
    if not stat.S_ISREG(mode):
        raise ConfigurationError(f"secret key file {key_file} is not a regular file")
    if mode & _FORBIDDEN_MODE_BITS:
        raise ConfigurationError(
            f"secret key file {key_file} must not be accessible by group or others "
            f"(mode {stat.S_IMODE(mode):04o}); run: chmod 600 {key_file}"
        )
