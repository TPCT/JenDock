"""Password hashing with argon2id."""

from __future__ import annotations

import secrets

from argon2 import PasswordHasher as Argon2Hasher
from argon2.exceptions import InvalidHashError, VerificationError


class PasswordHasher:
    """Hashes and verifies passwords; the argon2 parameters are injectable for tests."""

    def __init__(self, argon2: Argon2Hasher | None = None) -> None:
        self._argon2 = argon2 or Argon2Hasher()
        self._dummy_hash = self._argon2.hash(secrets.token_urlsafe(16))

    def hash(self, password: str) -> str:
        """Return the encoded argon2id hash of ``password``."""
        return self._argon2.hash(password)

    def verify(self, password_hash: str, password: str) -> bool:
        """Return whether ``password`` matches; malformed hashes never match."""
        try:
            return self._argon2.verify(password_hash, password)
        except (VerificationError, InvalidHashError):
            return False

    def needs_rehash(self, password_hash: str) -> bool:
        """Return whether the hash uses weaker parameters than the current policy."""
        return self._argon2.check_needs_rehash(password_hash)

    def verify_dummy(self, password: str) -> None:
        """Burn the same time as a real verification, for unknown users."""
        self.verify(self._dummy_hash, password)
