"""ACME v2 order flow: account, order, challenge, finalize, download."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from backend.adapters.acme.jws import AccountKey, b64url
from backend.adapters.errors import AdapterError

JOSE_CONTENT_TYPE = "application/jose+json"
PEM_CHAIN_TYPE = "application/pem-certificate-chain"
MAX_NONCE_RETRIES = 3
POLL_INTERVAL = 2.0
MAX_ERROR_TEXT = 300


class AcmeError(AdapterError):
    """The CA refused something or an order did not complete; the message holds no key material."""

    def __init__(self, message: str, problem: str | None = None) -> None:
        super().__init__(message)
        self.problem = problem


class ChallengeSolver(Protocol):
    """Proves control of a name for one challenge type (``dns-01`` or ``http-01``)."""

    challenge_type: str

    def present(self, domain: str, token: str, key_authorization: str) -> None:
        """Publish the proof so the CA can validate it."""

    def cleanup(self, domain: str, token: str, key_authorization: str) -> None:
        """Remove the proof again (called even when validation failed)."""


@dataclass(frozen=True, slots=True)
class AcmeConfig:
    """Connection settings of one CA."""

    directory_url: str
    email: str | None = None
    poll_timeout: float = 180.0
    poll_interval: float = POLL_INTERVAL


class AcmeClient:
    """Obtains certificates for a CSR from an ACME CA."""

    def __init__(
        self,
        config: AcmeConfig,
        account_key: AccountKey,
        http: httpx.Client,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._key = account_key
        self._http = http
        self._sleep = sleep
        self._monotonic = monotonic
        self._directory: Mapping[str, Any] | None = None
        self._nonce: str | None = None
        self._kid: str | None = None

    def obtain_certificate(
        self, identifiers: Sequence[str], csr_der: bytes, solver: ChallengeSolver
    ) -> str:
        """Run a complete order and return the certificate chain as PEM text."""
        self._ensure_account()
        order, order_url = self._new_order(identifiers)
        for authz_url in order["authorizations"]:
            self._authorize(str(authz_url), solver)
        order = self._wait_for_order(order_url, {"ready", "valid"})
        if order["status"] != "valid":
            order = self._finalize(order, order_url, csr_der)
        return self._download(str(order["certificate"]))

    # --- account and order ---------------------------------------------------------------

    def _ensure_account(self) -> None:
        if self._kid is not None:
            return
        payload: dict[str, object] = {"termsOfServiceAgreed": True}
        if self._config.email:
            payload["contact"] = [f"mailto:{self._config.email}"]
        response = self._post(self._url("newAccount"), payload, use_kid=False)
        location = response.headers.get("Location")
        if not location:
            raise AcmeError("the CA did not return an account location")
        self._kid = location

    def _new_order(self, identifiers: Sequence[str]) -> tuple[dict[str, Any], str]:
        payload = {"identifiers": [{"type": "dns", "value": name} for name in identifiers]}
        response = self._post(self._url("newOrder"), payload)
        location = response.headers.get("Location")
        if not location:
            raise AcmeError("the CA did not return an order location")
        return response.json(), location

    def _authorize(self, authz_url: str, solver: ChallengeSolver) -> None:
        authz = self._post(authz_url, None).json()
        if authz.get("status") == "valid":
            return
        challenge = next(
            (c for c in authz.get("challenges", []) if c.get("type") == solver.challenge_type), None
        )
        if challenge is None:
            raise AcmeError(f"the CA offers no {solver.challenge_type} challenge for this name")
        domain = str(authz["identifier"]["value"])
        token = str(challenge["token"])
        key_authorization = self._key.key_authorization(token)
        solver.present(domain, token, key_authorization)
        try:
            self._post(str(challenge["url"]), {})
            self._wait_for_authorization(authz_url)
        finally:
            solver.cleanup(domain, token, key_authorization)

    def _wait_for_authorization(self, authz_url: str) -> None:
        deadline = self._monotonic() + self._config.poll_timeout
        while True:
            authz = self._post(authz_url, None).json()
            status = authz.get("status")
            if status == "valid":
                return
            if status not in {"pending", "processing"}:
                raise AcmeError(_challenge_failure(authz), _challenge_problem(authz))
            if self._monotonic() >= deadline:
                raise AcmeError("timed out waiting for the CA to validate the challenge")
            self._sleep(self._config.poll_interval)

    def _wait_for_order(self, order_url: str, accepted: set[str]) -> dict[str, Any]:
        deadline = self._monotonic() + self._config.poll_timeout
        while True:
            order = self._post(order_url, None).json()
            status = order.get("status")
            if status in accepted:
                return dict(order)
            if status == "invalid":
                raise AcmeError(
                    _problem_text(order.get("error")), _problem_type(order.get("error"))
                )
            if self._monotonic() >= deadline:
                raise AcmeError("timed out waiting for the order")
            self._sleep(self._config.poll_interval)

    def _finalize(self, order: Mapping[str, Any], order_url: str, csr_der: bytes) -> dict[str, Any]:
        self._post(str(order["finalize"]), {"csr": b64url(csr_der)})
        return self._wait_for_order(order_url, {"valid"})

    def _download(self, certificate_url: str) -> str:
        response = self._post(certificate_url, None, accept=PEM_CHAIN_TYPE)
        text = response.text
        if "BEGIN CERTIFICATE" not in text:
            raise AcmeError("the CA returned no certificate")
        return text

    # --- transport -----------------------------------------------------------------------

    def _url(self, name: str) -> str:
        if self._directory is None:
            response = self._http.get(self._config.directory_url)
            _raise_for_status(response)
            self._directory = response.json()
        assert self._directory is not None  # noqa: S101 - narrowed for the type checker
        url = self._directory.get(name)
        if not isinstance(url, str):
            raise AcmeError(f"the ACME directory has no {name} endpoint")
        return url

    def _fresh_nonce(self) -> str:
        stashed = self._nonce
        if stashed is not None:
            self._nonce = None
            return stashed
        response = self._http.head(self._url("newNonce"))
        fetched = response.headers.get("Replay-Nonce")
        if not fetched:
            raise AcmeError("the CA did not provide a nonce")
        return str(fetched)

    def _post(
        self,
        url: str,
        payload: Mapping[str, object] | None,
        *,
        use_kid: bool = True,
        accept: str | None = None,
    ) -> httpx.Response:
        body = None if payload is None else json.dumps(payload).encode()
        for attempt in range(MAX_NONCE_RETRIES):
            protected: dict[str, object] = {
                "alg": "ES256",
                "nonce": self._fresh_nonce(),
                "url": url,
            }
            if use_kid:
                protected["kid"] = self._kid
            else:
                protected["jwk"] = self._key.jwk
            headers = {"Content-Type": JOSE_CONTENT_TYPE}
            if accept:
                headers["Accept"] = accept
            response = self._http.post(url, json=self._key.sign(protected, body), headers=headers)
            self._nonce = response.headers.get("Replay-Nonce")
            bad_nonce = response.status_code == 400 and _problem_of(response) == "badNonce"
            if bad_nonce and attempt + 1 < MAX_NONCE_RETRIES:
                continue
            _raise_for_status(response)
            return response
        raise AcmeError("the CA kept rejecting the nonce")  # pragma: no cover


def _problem_of(response: httpx.Response) -> str | None:
    try:
        data = response.json()
    except ValueError:
        return None
    return _problem_type(data) if isinstance(data, dict) else None


def _problem_type(problem: object) -> str | None:
    if not isinstance(problem, dict):
        return None
    kind = problem.get("type")
    return str(kind).rsplit(":", 1)[-1] if kind else None


def _problem_text(problem: object) -> str:
    if not isinstance(problem, dict):
        return "the CA rejected the request"
    detail = str(problem.get("detail", "the CA rejected the request"))
    return detail[:MAX_ERROR_TEXT]


def _raise_for_status(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    try:
        problem: object = response.json()
    except ValueError:
        problem = None
    message = _problem_text(problem) if problem else f"HTTP {response.status_code}"
    raise AcmeError(f"ACME error: {message}", _problem_type(problem))


def _challenge_failure(authz: Mapping[str, Any]) -> str:
    for challenge in authz.get("challenges", []):
        if challenge.get("error"):
            return f"challenge failed: {_problem_text(challenge['error'])}"
    return f"authorization ended with status {authz.get('status')}"


def _challenge_problem(authz: Mapping[str, Any]) -> str | None:
    for challenge in authz.get("challenges", []):
        if challenge.get("error"):
            return _problem_type(challenge["error"])
    return None
