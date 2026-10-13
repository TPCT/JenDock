"""Ingress value types: enums, ports, DNS targets and the preview/apply plan."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from backend.domain.errors import ValidationError
from backend.domain.hostnames import Hostname

MANAGED_TAG = "managed-by=jendock"
MIN_PORT = 1
MAX_PORT = 65535
MIN_PUBLISHED_PORT = 1024
CLOUDFLARE_TOKEN_SECRET = "CLOUDFLARE_API_TOKEN"  # noqa: S105 - the secret's name, not a value


class DomainStatus(StrEnum):
    """Where a domain is in its life cycle."""

    DRAFT = "draft"  # defined, nothing applied to DNS or Apache
    ACTIVE = "active"  # DNS record and vhost applied
    ERROR = "error"  # the last apply failed; ``status_message`` says why


class DomainOrigin(StrEnum):
    """Who set the domain up on the server."""

    MANAGED = "managed"  # created by Jendock: DNS record, Apache site and certificate are its own
    ADOPTED = "adopted"  # existed before; only recorded here, DNS and Apache stay untouched


class DnsRecordType(StrEnum):
    """Record types the platform manages."""

    A = "A"
    AAAA = "AAAA"
    CNAME = "CNAME"


class PlanAction(StrEnum):
    """What applying a plan part would do."""

    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    NOOP = "noop"
    CONFLICT = "conflict"  # a record the platform does not own is in the way


class CertificateState(StrEnum):
    """Expiry health of a certificate."""

    OK = "ok"
    RENEWAL_DUE = "renewal_due"  # within the renewal window, a renewal is expected
    WARNING = "warning"  # renewal is overdue and less than 14 days remain
    CRITICAL = "critical"  # less than 3 days remain
    EXPIRED = "expired"


def validate_port(value: int, name: str, *, minimum: int = MIN_PORT) -> int:
    """Return ``value`` if it is a valid TCP port (>= ``minimum``)."""
    if isinstance(value, bool) or not minimum <= value <= MAX_PORT:
        raise ValidationError(f"{name} must be between {minimum} and {MAX_PORT}")
    return value


def validate_dns_target(record_type: DnsRecordType, target: str | None) -> str | None:
    """Check an explicit DNS target against the record type; ``None`` means the server default."""
    if target is None:
        if record_type is DnsRecordType.CNAME:
            raise ValidationError("a CNAME record needs an explicit target")
        return None
    text = target.strip()
    try:
        if record_type is DnsRecordType.A:
            ipaddress.IPv4Address(text)
        elif record_type is DnsRecordType.AAAA:
            ipaddress.IPv6Address(text)
        else:
            text = Hostname.parse(text).value
    except ValueError as error:
        raise ValidationError(f"invalid {record_type.value} target: {target!r}") from error
    return text


@dataclass(frozen=True, slots=True)
class DnsRecordView:
    """A DNS record as shown in a preview."""

    record_type: str
    name: str
    content: str
    proxied: bool


@dataclass(frozen=True, slots=True)
class DnsPlanView:
    """The DNS part of a preview: what exists, what is wanted and what would happen."""

    action: PlanAction
    zone: str
    current: DnsRecordView | None
    desired: DnsRecordView | None
    removes: tuple[DnsRecordView, ...] = ()
    message: str = ""


@dataclass(frozen=True, slots=True)
class VhostPlanView:
    """The Apache part of a preview: both texts and a unified diff."""

    action: PlanAction
    current: str | None
    proposed: str | None
    diff: str


@dataclass(frozen=True, slots=True)
class TlsPlanView:
    """The certificate part of a preview."""

    action: str  # "issue" | "keep" | "remove" | "none"
    challenge: str | None = None
    not_after: datetime | None = None


@dataclass(frozen=True, slots=True)
class IngressPlanView:
    """Everything that would change; ``plan_hash`` must be sent back to apply it."""

    domain_id: int
    hostname: str
    operation: str  # "apply" | "remove"
    dns: DnsPlanView
    vhost: VhostPlanView
    tls: TlsPlanView
    steps: tuple[str, ...]
    plan_hash: str

    @property
    def blocked(self) -> bool:
        """Whether the plan cannot be applied (a foreign DNS record is in the way)."""
        return self.dns.action is PlanAction.CONFLICT
