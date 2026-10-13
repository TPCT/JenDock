"""Network probes for domain status checks: DNS resolution and HTTP(S) requests."""

from __future__ import annotations

import http.client
import socket
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

MAX_BODY = 4096


class DnsResolver(Protocol):
    """Resolves a hostname to its addresses."""

    def resolve(self, hostname: str) -> list[str]:
        """Return the distinct A/AAAA addresses; empty when the name does not resolve."""


@dataclass(frozen=True, slots=True)
class HttpResult:
    """Outcome of one request to the public hostname."""

    reached: bool
    status: int | None = None
    tls_valid: bool | None = None
    not_after: datetime | None = None
    detail: str = ""


class HttpProber(Protocol):
    """Requests ``/`` of a hostname the way a browser would, verifying the certificate."""

    def probe(self, hostname: str, *, tls: bool, timeout: float) -> HttpResult:
        """Make the request; never raises for network failures."""


class SocketDnsResolver:
    """``DnsResolver`` over the system resolver."""

    def resolve(self, hostname: str) -> list[str]:
        """Resolve with ``getaddrinfo`` (TCP entries only, de-duplicated, sorted)."""
        try:
            infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
        except OSError:
            return []
        return sorted({str(info[4][0]) for info in infos})


class StdlibHttpProber:
    """``HttpProber`` over ``http.client`` with the default (verifying) TLS context."""

    method = "GET"

    def probe(self, hostname: str, *, tls: bool, timeout: float) -> HttpResult:
        """Request ``/`` once; certificate and hostname are verified for HTTPS."""
        try:
            return self._request(hostname, tls=tls, timeout=timeout)
        except ssl.SSLCertVerificationError as error:
            return HttpResult(True, tls_valid=False, detail=f"certificate rejected: {error.reason}")
        except ssl.SSLError as error:
            return HttpResult(False, detail=f"TLS failure: {error.reason}")
        except (OSError, http.client.HTTPException) as error:
            return HttpResult(False, detail=f"no response: {error.__class__.__name__}")

    def _request(self, hostname: str, *, tls: bool, timeout: float) -> HttpResult:
        connection: http.client.HTTPConnection
        if tls:
            connection = http.client.HTTPSConnection(
                hostname, timeout=timeout, context=ssl.create_default_context()
            )
        else:
            connection = http.client.HTTPConnection(hostname, timeout=timeout)
        try:
            connection.request(self.method, "/", headers={"User-Agent": "jendock-domain-check"})
            response = connection.getresponse()
            response.read(MAX_BODY)
            return HttpResult(
                True,
                status=response.status,
                tls_valid=True if tls else None,
                not_after=_not_after(connection) if tls else None,
            )
        finally:
            connection.close()


class StdlibHeadProber(StdlibHttpProber):
    """The same probe with ``HEAD``: no body is transferred, which is all a liveness check needs."""

    method = "HEAD"


def _not_after(connection: http.client.HTTPConnection) -> datetime | None:
    sock = connection.sock
    if not isinstance(sock, ssl.SSLSocket):
        return None
    cert = sock.getpeercert()
    expires = cert.get("notAfter") if cert else None
    if not isinstance(expires, str):
        return None
    return datetime.fromtimestamp(ssl.cert_time_to_seconds(expires), tz=UTC)
