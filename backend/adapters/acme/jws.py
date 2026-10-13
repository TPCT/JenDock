"""JSON Web Signatures (ES256) and the account key, as ACME needs them."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

COORDINATE_BYTES = 32
KEY_MODE = 0o600


def b64url(data: bytes) -> str:
    """Unpadded base64url, as used throughout ACME."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def load_or_create_account_key(path: Path) -> ec.EllipticCurvePrivateKey:
    """Load the ACME account key, creating a new P-256 key (mode 0600) when none exists."""
    try:
        return _load(path)
    except FileNotFoundError:
        pass
    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, KEY_MODE)
    except FileExistsError:
        return _load(path)  # another thread or process created it first
    with os.fdopen(fd, "wb") as handle:
        handle.write(pem)
    return key


def _load(path: Path) -> ec.EllipticCurvePrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise ValueError("ACME account key must be an EC key")
    return key


class AccountKey:
    """An ES256 signing key with its JWK and thumbprint."""

    def __init__(self, key: ec.EllipticCurvePrivateKey) -> None:
        self._key = key
        numbers = key.public_key().public_numbers()
        self.jwk: dict[str, str] = {
            "crv": "P-256",
            "kty": "EC",
            "x": b64url(numbers.x.to_bytes(COORDINATE_BYTES, "big")),
            "y": b64url(numbers.y.to_bytes(COORDINATE_BYTES, "big")),
        }

    @property
    def thumbprint(self) -> str:
        """RFC 7638 thumbprint, the suffix of every key authorization."""
        canonical = json.dumps(self.jwk, sort_keys=True, separators=(",", ":"))
        return b64url(hashlib.sha256(canonical.encode()).digest())

    def key_authorization(self, token: str) -> str:
        """The value a challenge must prove: ``token.thumbprint``."""
        return f"{token}.{self.thumbprint}"

    def sign(self, protected: dict[str, object], payload: bytes | None) -> dict[str, str]:
        """Build a flattened JWS; ``payload=None`` is the empty POST-as-GET payload."""
        protected_b64 = b64url(json.dumps(protected, separators=(",", ":")).encode())
        payload_b64 = "" if payload is None else b64url(payload)
        signing_input = f"{protected_b64}.{payload_b64}".encode("ascii")
        der = self._key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        raw = r.to_bytes(COORDINATE_BYTES, "big") + s.to_bytes(COORDINATE_BYTES, "big")
        return {"protected": protected_b64, "payload": payload_b64, "signature": b64url(raw)}
