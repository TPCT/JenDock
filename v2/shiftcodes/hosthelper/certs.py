"""Validation of certificate material before it is installed."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from cryptography.x509.oid import ExtensionOID

from shiftcodes.hosthelper.errors import InvalidRequestError
from shiftcodes.hosthelper.names import WILDCARD_PREFIX

MAX_PEM_BYTES = 128 * 1024
PEM_CERT_MARKER = b"-----BEGIN CERTIFICATE-----"
PEM_KEY_MARKER = b"-----BEGIN"
MAX_CHAIN_LENGTH = 6


@dataclass(frozen=True, slots=True)
class CertificateInfo:
    """What the helper learned about a validated certificate."""

    not_before: datetime
    not_after: datetime
    serial: str


def validate_material(hostname: str, fullchain_pem: str, private_key_pem: str) -> CertificateInfo:
    """Check chain, key and hostname coverage; return the leaf's validity window."""
    chain = _parse_chain(fullchain_pem)
    leaf = chain[0]
    key = _parse_key(private_key_pem)
    if not _same_public_key(leaf.public_key(), key.public_key()):
        raise InvalidRequestError("private key does not match the certificate")
    if not _covers(leaf, hostname):
        raise InvalidRequestError("certificate does not cover the hostname")
    now = datetime.now(UTC)
    if leaf.not_valid_after_utc <= now or leaf.not_valid_before_utc > now:
        raise InvalidRequestError("certificate is not currently valid")
    return _info(leaf)


def describe_certificate(fullchain_pem: str) -> CertificateInfo:
    """Return the validity window of the leaf certificate of an installed chain."""
    return _info(_parse_chain(fullchain_pem)[0])


def _info(leaf: x509.Certificate) -> CertificateInfo:
    return CertificateInfo(
        leaf.not_valid_before_utc, leaf.not_valid_after_utc, f"{leaf.serial_number:x}"
    )


def _parse_chain(pem: str) -> list[x509.Certificate]:
    data = pem.encode()
    if len(data) > MAX_PEM_BYTES or PEM_CERT_MARKER not in data:
        raise InvalidRequestError("invalid certificate chain")
    try:
        chain = x509.load_pem_x509_certificates(data)
    except ValueError as error:
        raise InvalidRequestError("invalid certificate chain") from error
    if not chain or len(chain) > MAX_CHAIN_LENGTH:
        raise InvalidRequestError("invalid certificate chain length")
    return chain


PrivateKey = ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey | ed25519.Ed25519PrivateKey


def _parse_key(pem: str) -> PrivateKey:
    data = pem.encode()
    if len(data) > MAX_PEM_BYTES or PEM_KEY_MARKER not in data:
        raise InvalidRequestError("invalid private key")
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError) as error:
        raise InvalidRequestError("invalid or encrypted private key") from error
    if not isinstance(
        key, ec.EllipticCurvePrivateKey | rsa.RSAPrivateKey | ed25519.Ed25519PrivateKey
    ):
        raise InvalidRequestError("unsupported private key type")
    return key


def _same_public_key(first: object, second: object) -> bool:
    def encode(key: object) -> bytes:
        if not isinstance(
            key, ec.EllipticCurvePublicKey | rsa.RSAPublicKey | ed25519.Ed25519PublicKey
        ):
            return b""
        return key.public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    one, two = encode(first), encode(second)
    return bool(one) and one == two


def _covers(leaf: x509.Certificate, hostname: str) -> bool:
    try:
        san = leaf.extensions.get_extension_for_oid(ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
    except x509.ExtensionNotFound:
        return False
    names = san.value.get_values_for_type(x509.DNSName)  # type: ignore[attr-defined]
    if hostname in names:
        return True
    if hostname.startswith(WILDCARD_PREFIX):
        return False
    _, _, parent = hostname.partition(".")
    return f"{WILDCARD_PREFIX}{parent}" in names
