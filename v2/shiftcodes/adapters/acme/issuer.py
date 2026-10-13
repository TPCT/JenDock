"""Turns an ACME order into a fresh key and certificate chain."""

from __future__ import annotations

from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from shiftcodes.adapters.acme.client import AcmeClient, ChallengeSolver


@dataclass(frozen=True, slots=True)
class IssuedCertificate:
    """A certificate chain with the key it was issued for (never persisted by the platform)."""

    fullchain_pem: str
    private_key_pem: str = ""


class AcmeIssuer:
    """Creates a new P-256 key per issuance, so every renewal rotates the key."""

    def __init__(self, client: AcmeClient, solver: ChallengeSolver) -> None:
        self._client = client
        self._solver = solver

    @property
    def challenge_type(self) -> str:
        """The challenge type this issuer solves."""
        return self._solver.challenge_type

    def issue(self, hostname: str) -> IssuedCertificate:
        """Obtain a certificate for ``hostname`` (``*.zone`` names need DNS-01)."""
        key = ec.generate_private_key(ec.SECP256R1())
        csr = (
            x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname[:64])]))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
            .sign(key, hashes.SHA256())
        )
        chain = self._client.obtain_certificate(
            [hostname], csr.public_bytes(serialization.Encoding.DER), self._solver
        )
        key_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode("ascii")
        return IssuedCertificate(chain, key_pem)
