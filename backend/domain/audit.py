"""Audit value types and the rule that keeps secrets out of audit details."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from backend.domain.actor import Actor
from backend.domain.errors import ValidationError
from backend.logging import SENSITIVE_KEYS

# JSON-like values are heterogeneous by nature, so the alias uses object.
AuditDetails = dict[str, object]


@dataclass(frozen=True, slots=True)
class AuditTarget:
    """The entity an audited action was applied to."""

    type: str
    id: str | None = None


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """Everything needed to record one audit event."""

    actor: Actor
    action: str
    target: AuditTarget
    details: AuditDetails


@dataclass(frozen=True, slots=True)
class AuditFilter:
    """Search criteria of the audit log; every field left ``None`` matches everything.

    ``action`` is exact, or a prefix when it ends in ``*`` (``"secret.*"``). ``project`` is a
    project slug: it matches the project itself and its environments, runs, deployments,
    domains and database connections. ``involving_user`` limits to what a user did or what
    was done to their account (the "security log" of the profile). ``text`` searches the
    action, target id, details and the name of user actors.
    """

    text: str | None = None
    actor_type: str | None = None
    actor_id: str | None = None
    action: str | None = None
    target_type: str | None = None
    target_id: str | None = None
    project: str | None = None
    since: datetime | None = None
    until: datetime | None = None
    involving_user: int | None = None
    # Member scope (set by ``authz.audit_filter``, never by a request): only events the user
    # caused or that concern their own account, and of those only the ones that are not about a
    # project, or are about one of ``visible_projects``.
    restrict_user: int | None = None
    visible_projects: tuple[str, ...] = ()


def validate_audit_details(details: AuditDetails) -> AuditDetails:
    """Reject details whose keys (at any depth) look sensitive; never sanitize silently."""
    _check_value(details, "details")
    return details


def _check_value(value: object, path: str) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            _check_key(str(key), path)
            _check_value(item, f"{path}.{key}")
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _check_value(item, f"{path}[{index}]")


def _check_key(key: str, path: str) -> None:
    lowered = key.lower()
    if any(marker in lowered for marker in SENSITIVE_KEYS):
        raise ValidationError(f"audit details key {key!r} at {path} is sensitive")
