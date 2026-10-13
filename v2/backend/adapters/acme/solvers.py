"""Challenge solvers: DNS-01 through Cloudflare (primary) and HTTP-01 through the host helper."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable

from backend.adapters.acme.jws import b64url
from backend.adapters.cloudflare import CloudflareClient
from backend.adapters.host_helper_client import HostHelper
from backend.domain.ingress import MANAGED_TAG

ACME_LABEL = "_acme-challenge"
TXT_COMMENT = f"{MANAGED_TAG} purpose=acme-dns-01"


def dns01_value(key_authorization: str) -> str:
    """The TXT value for a DNS-01 challenge (RFC 8555 section 8.4)."""
    return b64url(hashlib.sha256(key_authorization.encode("ascii")).digest())


class Dns01CloudflareSolver:
    """Publishes ``_acme-challenge`` TXT records in the domain's Cloudflare zone.

    Only TXT records carrying the platform tag are ever deleted, so foreign records stay.
    """

    challenge_type = "dns-01"

    def __init__(
        self,
        cloudflare: CloudflareClient,
        *,
        propagation_wait: float = 5.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._cloudflare = cloudflare
        self._wait = propagation_wait
        self._sleep = sleep

    def present(self, domain: str, token: str, key_authorization: str) -> None:  # noqa: ARG002
        """Create the TXT record unless an identical one already exists."""
        name = f"{ACME_LABEL}.{domain}"
        value = dns01_value(key_authorization)
        zone = self._cloudflare.find_zone(domain)
        existing = self._cloudflare.list_records(zone.id, name)
        if not any(r.record_type == "TXT" and r.content.strip('"') == value for r in existing):
            body = {"type": "TXT", "name": name, "content": value, "ttl": 1, "comment": TXT_COMMENT}
            self._cloudflare.create_record(zone.id, body)
        self._sleep(self._wait)

    def cleanup(self, domain: str, token: str, key_authorization: str) -> None:  # noqa: ARG002
        """Delete the platform's TXT record for this challenge."""
        name = f"{ACME_LABEL}.{domain}"
        value = dns01_value(key_authorization)
        zone = self._cloudflare.find_zone(domain)
        for record in self._cloudflare.list_records(zone.id, name):
            if record.record_type == "TXT" and record.owned and record.content.strip('"') == value:
                self._cloudflare.delete_record(zone.id, record.id)


class Http01HelperSolver:
    """Serves the key authorization from the helper's webroot (``/.well-known/acme-challenge``)."""

    challenge_type = "http-01"

    def __init__(self, helper: HostHelper) -> None:
        self._helper = helper

    def present(self, domain: str, token: str, key_authorization: str) -> None:  # noqa: ARG002
        """Write the challenge file on the host."""
        self._helper.put_challenge(token, key_authorization)

    def cleanup(self, domain: str, token: str, key_authorization: str) -> None:  # noqa: ARG002
        """Remove the challenge file."""
        self._helper.remove_challenge(token)
