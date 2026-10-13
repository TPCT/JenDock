"""Short-lived, single-use tickets that authorize one WebSocket terminal connection.

A browser cannot send an ``Authorization`` header or a CSRF token on a WebSocket handshake, so
the page first calls a CSRF-protected ``POST`` (which authenticates normally and checks the
container) and receives a ticket. The WebSocket then presents the ticket in its first message.
Tickets live in the memory of the API process (which runs as one process), expire after 30
seconds and work exactly once.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from shiftcodes.domain.actor import Actor
from shiftcodes.domain.clock import Clock, utcnow
from shiftcodes.domain.enums import EnvironmentName
from shiftcodes.domain.errors import RateLimitedError

TICKET_TTL = timedelta(seconds=30)
MAX_PER_PRINCIPAL = 10
MAX_TOTAL = 1000


class PrincipalKind(StrEnum):
    """Who the ticket was issued to."""

    SESSION = "session"
    TOKEN = "token"  # noqa: S105 - kind of principal, not a credential


@dataclass(frozen=True, slots=True)
class TicketGrant:
    """What a ticket allows: one terminal in one container, for one principal."""

    kind: PrincipalKind
    principal_id: int
    actor: Actor
    slug: str
    env: EnvironmentName
    container_id: str
    terminal_id: str | None
    expires_at: datetime


class TicketStore:
    """Issues and redeems tickets."""

    def __init__(self, clock: Clock = utcnow) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._grants: dict[str, TicketGrant] = {}

    def issue(self, grant: TicketGrant) -> str:
        """Store ``grant`` and return its ticket; a principal may hold a few at a time."""
        ticket = secrets.token_urlsafe(32)
        with self._lock:
            self._purge()
            mine = sum(
                1
                for item in self._grants.values()
                if (item.kind, item.principal_id) == (grant.kind, grant.principal_id)
            )
            if mine >= MAX_PER_PRINCIPAL or len(self._grants) >= MAX_TOTAL:
                raise RateLimitedError("too many pending terminal tickets", retry_after=30)
            self._grants[_digest(ticket)] = grant
        return ticket

    def redeem(self, ticket: str) -> TicketGrant | None:
        """Return the grant and invalidate the ticket; ``None`` when unknown, used or expired."""
        with self._lock:
            grant = self._grants.pop(_digest(ticket), None)
        if grant is None or grant.expires_at <= self._clock():
            return None
        return grant

    def new_expiry(self) -> datetime:
        """Expiry time for a ticket issued now."""
        return self._clock() + TICKET_TTL

    def _purge(self) -> None:
        now = self._clock()
        for key in [k for k, grant in self._grants.items() if grant.expires_at <= now]:
            del self._grants[key]


def _digest(ticket: str) -> str:
    return hashlib.sha256(ticket.encode()).hexdigest()
