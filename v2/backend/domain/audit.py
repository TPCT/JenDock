"""Audit value types and the rule that keeps secrets out of audit details."""

from __future__ import annotations

from dataclasses import dataclass

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
