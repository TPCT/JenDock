"""A small in-process ACME CA (RFC 8555 subset) behind ``httpx.MockTransport``.

It verifies every JWS signature against the account key, tracks nonces, lets the test decide
whether a challenge validates (by looking at what the solver published), and signs the CSR
with a throw-away CA. It exists to test the client without a network; the real protocol is
exercised against Pebble in ``test_acme_pebble_docker.py``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.x509.oid import NameOID

BASE = "https://ca.test"
Verifier = Callable[[str, str, str, str], bool]


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _problem(status: int, kind: str, detail: str) -> httpx.Response:
    body = {"type": f"urn:ietf:params:acme:error:{kind}", "detail": detail}
    return httpx.Response(status, json=body, headers={"Content-Type": "application/problem+json"})


class FakeAcmeCa:
    """The CA: ``transport()`` gives the ``httpx`` transport, ``verifier`` validates challenges."""

    def __init__(
        self, verifier: Verifier, challenge_types: tuple[str, ...] = ("dns-01", "http-01")
    ) -> None:
        self.verifier = verifier
        self.challenge_types = challenge_types
        self.nonces: set[str] = set()
        self.bad_nonce_once = False
        self.pending_polls = 1  # authz stays "pending" for this many polls after being triggered
        self.accounts: dict[str, dict[str, str]] = {}
        self.orders: dict[str, dict[str, Any]] = {}
        self.authzs: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str]] = []
        self.new_account_calls = 0
        self._ca_key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Fake Test CA")])
        now = datetime.now(UTC)
        self._ca_cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(self._ca_key.public_key())
            .serial_number(1)
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=3650))
            .sign(self._ca_key, hashes.SHA256())
        )

    def transport(self) -> httpx.MockTransport:
        """The transport to build an ``httpx.Client`` on."""
        return httpx.MockTransport(self.handle)

    # --- routing ---------------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        """Serve one request."""
        path = request.url.path
        self.requests.append((request.method, path))
        if path == "/directory":
            return self._with_nonce(httpx.Response(200, json=self._directory()))
        if path == "/nonce":
            return self._with_nonce(httpx.Response(200))
        payload, error = self._verify(request)
        if error is not None:
            return self._with_nonce(error)
        routes: dict[str, Callable[[Any, httpx.Request], httpx.Response]] = {
            "/new-account": self._new_account,
            "/new-order": self._new_order,
        }
        if path in routes:
            return self._with_nonce(routes[path](payload, request))
        kind, _, ident = path.strip("/").partition("/")
        handler = {
            "authz": self._authz,
            "chall": self._challenge,
            "order": self._order,
            "finalize": self._finalize,
            "cert": self._cert,
        }.get(kind)
        if handler is None:
            return self._with_nonce(_problem(404, "malformed", "no such resource"))
        return self._with_nonce(handler(ident, payload))

    def _directory(self) -> dict[str, str]:
        return {
            "newNonce": f"{BASE}/nonce",
            "newAccount": f"{BASE}/new-account",
            "newOrder": f"{BASE}/new-order",
        }

    def _with_nonce(self, response: httpx.Response) -> httpx.Response:
        nonce = secrets.token_urlsafe(16)
        self.nonces.add(nonce)
        response.headers["Replay-Nonce"] = nonce
        return response

    # --- JWS -------------------------------------------------------------------------------

    def _verify(self, request: httpx.Request) -> tuple[Any, httpx.Response | None]:
        body = json.loads(request.content)
        protected = json.loads(_b64d(body["protected"]))
        if protected["url"] != str(request.url):
            return None, _problem(400, "malformed", "url mismatch")
        nonce = protected["nonce"]
        if self.bad_nonce_once:
            self.bad_nonce_once = False
            self.nonces.discard(nonce)
            return None, _problem(400, "badNonce", "JWS has an invalid anti-replay nonce")
        if nonce not in self.nonces:
            return None, _problem(400, "badNonce", "unknown nonce")
        self.nonces.discard(nonce)
        jwk = protected.get("jwk") or self.accounts.get(protected.get("kid", ""))
        if not jwk:
            return None, _problem(400, "accountDoesNotExist", "unknown account")
        public = ec.EllipticCurvePublicNumbers(
            int.from_bytes(_b64d(jwk["x"]), "big"),
            int.from_bytes(_b64d(jwk["y"]), "big"),
            ec.SECP256R1(),
        ).public_key()
        signature = _b64d(body["signature"])
        der = encode_dss_signature(
            int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")
        )
        signing_input = f"{body['protected']}.{body['payload']}".encode("ascii")
        try:
            public.verify(der, signing_input, ec.ECDSA(hashes.SHA256()))
        except Exception:
            return None, _problem(400, "malformed", "JWS verification error")
        self._current_jwk = jwk
        payload = json.loads(_b64d(body["payload"])) if body["payload"] else None
        return payload, None

    # --- resources -------------------------------------------------------------------------

    def _new_account(self, payload: Any, request: httpx.Request) -> httpx.Response:
        assert payload["termsOfServiceAgreed"] is True
        self.new_account_calls += 1
        kid = f"{BASE}/acct/{len(self.accounts) + 1}"
        self.accounts[kid] = self._current_jwk
        return httpx.Response(201, json={"status": "valid"}, headers={"Location": kid})

    def _new_order(self, payload: Any, request: httpx.Request) -> httpx.Response:
        order_id = secrets.token_hex(4)
        authz_urls = []
        for ident in payload["identifiers"]:
            authz_id = secrets.token_hex(4)
            tokens = {t: secrets.token_urlsafe(32) for t in self.challenge_types}
            self.authzs[authz_id] = {
                "status": "pending",
                "identifier": ident,
                "tokens": tokens,
                "polls_left": None,
                "order": order_id,
            }
            authz_urls.append(f"{BASE}/authz/{authz_id}")
        self.orders[order_id] = {
            "status": "pending",
            "identifiers": payload["identifiers"],
            "authorizations": authz_urls,
            "finalize": f"{BASE}/finalize/{order_id}",
        }
        body = self._order_body(order_id)
        return httpx.Response(201, json=body, headers={"Location": f"{BASE}/order/{order_id}"})

    def _order_body(self, order_id: str) -> dict[str, Any]:
        order = self.orders[order_id]
        statuses = [self.authzs[u.rsplit("/", 1)[1]]["status"] for u in order["authorizations"]]
        if order["status"] == "pending" and all(s == "valid" for s in statuses):
            order["status"] = "ready"
        if any(s == "invalid" for s in statuses):
            order["status"] = "invalid"
        body = {k: v for k, v in order.items() if k != "certificate_pem"}
        if order["status"] == "valid":
            body["certificate"] = f"{BASE}/cert/{order_id}"
        return body

    def _authz(self, authz_id: str, _payload: Any) -> httpx.Response:
        authz = self.authzs[authz_id]
        if authz["status"] == "pending" and authz["polls_left"] is not None:
            authz["polls_left"] -= 1
            if authz["polls_left"] < 0:
                self._settle(authz)
        challenges = [
            {
                "type": kind,
                "url": f"{BASE}/chall/{authz_id}:{kind}",
                "token": token,
                "status": authz["status"],
                **({"error": authz["error"]} if authz.get("error") else {}),
            }
            for kind, token in authz["tokens"].items()
        ]
        body = {
            "status": authz["status"],
            "identifier": authz["identifier"],
            "challenges": challenges,
        }
        return httpx.Response(200, json=body)

    def _challenge(self, ident: str, _payload: Any) -> httpx.Response:
        authz_id, kind = ident.split(":")
        authz = self.authzs[authz_id]
        authz["polls_left"] = self.pending_polls
        authz["chosen"] = kind
        return httpx.Response(200, json={"type": kind, "status": "processing"})

    def _settle(self, authz: dict[str, Any]) -> None:
        kind = authz["chosen"]
        token = authz["tokens"][kind]
        thumbprint = _thumbprint(self._current_jwk)
        key_authorization = f"{token}.{thumbprint}"
        if self.verifier(kind, authz["identifier"]["value"], token, key_authorization):
            authz["status"] = "valid"
        else:
            authz["status"] = "invalid"
            authz["error"] = {
                "type": "urn:ietf:params:acme:error:unauthorized",
                "detail": "the proof was not found",
            }

    def _order(self, order_id: str, _payload: Any) -> httpx.Response:
        return httpx.Response(200, json=self._order_body(order_id))

    def _finalize(self, order_id: str, payload: Any) -> httpx.Response:
        order = self.orders[order_id]
        if order["status"] != "ready":
            return _problem(403, "orderNotReady", "order is not ready")
        csr = x509.load_der_x509_csr(_b64d(payload["csr"]))
        assert csr.is_signature_valid
        san = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = san.get_values_for_type(x509.DNSName)
        wanted = [i["value"] for i in order["identifiers"]]
        if sorted(names) != sorted(wanted):
            return _problem(400, "badCSR", "CSR names do not match the order")
        now = datetime.now(UTC)
        leaf = (
            x509.CertificateBuilder()
            .subject_name(csr.subject)
            .issuer_name(self._ca_cert.subject)
            .public_key(csr.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(days=90))
            .add_extension(san, critical=False)
            .sign(self._ca_key, hashes.SHA256())
        )
        pem = leaf.public_bytes(serialization.Encoding.PEM) + self._ca_cert.public_bytes(
            serialization.Encoding.PEM
        )
        order["certificate_pem"] = pem.decode()
        order["status"] = "valid"
        return httpx.Response(200, json=self._order_body(order_id))

    def _cert(self, order_id: str, _payload: Any) -> httpx.Response:
        pem = self.orders[order_id]["certificate_pem"]
        return httpx.Response(
            200, text=pem, headers={"Content-Type": "application/pem-certificate-chain"}
        )


def _thumbprint(jwk: dict[str, str]) -> str:
    canonical = json.dumps(
        {k: jwk[k] for k in ("crv", "kty", "x", "y")}, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(canonical.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
