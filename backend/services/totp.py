"""TOTP (RFC 6238) and the vault that keeps two-factor keys encrypted.

The key of a user is generated and decrypted only by the *worker* (``TotpVault`` runs there,
behind the ``totp.*`` RPC methods), exactly like project secrets: the API process stores the
ciphertext and never holds the key that opens it. The plaintext key leaves the vault once, in
the enrollment response, so the user can scan it; verification sends the ciphertext and the
code to the worker and gets back only the matching time step.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from collections.abc import Callable
from urllib.parse import quote, urlencode

from backend.services.secret_cipher import SecretCipher

DIGITS = 6
PERIOD_SECONDS = 30
WINDOW_STEPS = 1  # accept the previous and the next step for clock drift
KEY_BYTES = 20  # 160 bits, the length RFC 4226 recommends
ISSUER = "Jendock"
_CONTEXT = "totp-v1:"


def hotp(key: bytes, counter: int) -> str:
    """RFC 4226 HOTP value (HMAC-SHA1, 6 digits) for ``counter``."""
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def time_step(now: float) -> int:
    """The RFC 6238 time step (30 s) containing the Unix time ``now``."""
    return int(now // PERIOD_SECONDS)


def matching_step(key: bytes, code: str, now: float, after_step: int | None) -> int | None:
    """The time step ``code`` is valid for, or ``None``.

    Steps at or before ``after_step`` (the last accepted one) never match, so a code works
    once even inside its validity window.
    """
    if len(code) != DIGITS or not code.isascii() or not code.isdigit():
        return None
    current = time_step(now)
    found: int | None = None
    for step in range(current - WINDOW_STEPS, current + WINDOW_STEPS + 1):
        # Evaluate every candidate so the time does not depend on which one matches.
        if hmac.compare_digest(hotp(key, step), code) and found is None:
            found = step
    if found is None or (after_step is not None and found <= after_step):
        return None
    return found


def encode_key(key: bytes) -> str:
    """Base32 without padding, the form authenticator apps expect."""
    return base64.b32encode(key).decode("ascii").rstrip("=")


def decode_key(text: str) -> bytes:
    """Inverse of ``encode_key``."""
    return base64.b32decode(text + "=" * (-len(text) % 8))


def provisioning_uri(username: str, base32_key: str) -> str:
    """The ``otpauth://`` URI an authenticator app scans (also rendered as a QR code)."""
    label = quote(f"{ISSUER}:{username}", safe="")
    query = urlencode(
        {
            "secret": base32_key,
            "issuer": ISSUER,
            "algorithm": "SHA1",
            "digits": DIGITS,
            "period": PERIOD_SECONDS,
        }
    )
    return f"otpauth://totp/{label}?{query}"


class TotpVault:
    """Generates and checks keys; holds the cipher, so it lives in the worker only."""

    def __init__(self, cipher: SecretCipher, clock: Callable[[], float] = time.time) -> None:
        self._cipher = cipher
        self._clock = clock

    def generate(self) -> tuple[str, bytes]:
        """A new key: its base32 text (shown once) and its ciphertext (stored)."""
        text = encode_key(secrets.token_bytes(KEY_BYTES))
        return text, self._cipher.encrypt(_CONTEXT + text)

    def check(self, ciphertext: bytes, code: str, after_step: int | None) -> int | None:
        """The matching time step of ``code`` against the stored key, or ``None``."""
        text = self._cipher.decrypt(ciphertext)
        if not text.startswith(_CONTEXT):
            return None
        key = decode_key(text[len(_CONTEXT) :])
        return matching_step(key, code, self._clock(), after_step)
