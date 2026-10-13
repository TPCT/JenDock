"""Stored platform settings for ingress; the ``JENDOCK_INGRESS_*`` environment is the default.

Small and typed on purpose: only the values an operator needs to change from the product (the
ACME contact email, the server's public addresses and the ACME challenge). Anything else stays
environment-only. A stored value wins over the environment; removing it restores the default.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, fields
from datetime import datetime

from backend.config_ingress import AcmeChallenge, IngressSettings
from backend.domain.actor import Actor
from backend.domain.audit import AuditEntry, AuditTarget
from backend.domain.clock import Clock, utcnow
from backend.domain.errors import ValidationError
from backend.repositories.database import UnitOfWork
from backend.repositories.models import PlatformSetting
from backend.services.audit import add_audit_event

PREFIX = "ingress."
EMAIL = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,}$")
CHALLENGES: tuple[AcmeChallenge, ...] = ("dns-01", "http-01")


@dataclass(frozen=True, slots=True)
class IngressOverrides:
    """The stored values; ``None`` means "not stored, use the environment default"."""

    acme_email: str | None = None
    public_ipv4: str | None = None
    public_ipv6: str | None = None
    acme_challenge: str | None = None


@dataclass(frozen=True, slots=True)
class IngressSettingsPatch:
    """A change: only the names in ``present`` are touched; a ``None`` value resets one."""

    values: IngressOverrides
    present: frozenset[str]


def _validate(name: str, value: str) -> str:
    cleaned = value.strip()
    try:
        if name == "acme_email" and not EMAIL.match(cleaned):
            raise ValueError("enter a valid email address")
        if name == "public_ipv4":
            ipaddress.IPv4Address(cleaned)
        if name == "public_ipv6":
            ipaddress.IPv6Address(cleaned)
        if name == "acme_challenge" and cleaned not in CHALLENGES:
            raise ValueError("the challenge must be dns-01 or http-01")
    except ValueError as error:
        raise ValidationError(f"{name}: {error}") from error
    return cleaned


class PlatformSettingsService:
    """Reads and writes the stored ingress overrides."""

    def __init__(self, uow: UnitOfWork, clock: Clock = utcnow) -> None:
        self._uow = uow
        self._clock = clock

    def overrides(self) -> IngressOverrides:
        """The stored values."""
        with self._uow:
            stored = {s.key.removeprefix(PREFIX): s.value for s in self._uow.platform.settings()}
        names = {f.name for f in fields(IngressOverrides)}
        return IngressOverrides(**{k: v for k, v in stored.items() if k in names})

    def effective(self, base: IngressSettings) -> IngressSettings:
        """``base`` (the environment) with the stored values applied."""
        stored = self.overrides()
        update = {f.name: getattr(stored, f.name) for f in fields(stored)}
        return base.model_copy(update={k: v for k, v in update.items() if v is not None})

    def update(self, patch: IngressSettingsPatch, actor: Actor) -> IngressOverrides:
        """Store, replace or reset the named values; audited with the names only."""
        changed: list[str] = []
        with self._uow:
            for name in sorted(patch.present):
                if self._apply_one(name, getattr(patch.values, name)):
                    changed.append(name)
            entry = AuditEntry(
                actor,
                "integration.ingress_settings_update",
                AuditTarget("platform", "ingress"),
                {"changed": changed},
            )
            add_audit_event(self._uow, entry, self._clock)
        return self.overrides()

    def _apply_one(self, name: str, value: str | None) -> bool:
        key = PREFIX + name
        existing = self._uow.platform.get_setting(key)
        if value is None or value.strip() == "":
            if existing is None:
                return False
            self._uow.platform.delete_setting(existing)
            return True
        cleaned = _validate(name, value)
        now = self._clock()
        if existing is None:
            self._uow.platform.add_setting(PlatformSetting(key=key, value=cleaned, updated_at=now))
        else:
            existing.value = cleaned
            existing.updated_at = now
        return True

    def last_update(self) -> datetime | None:
        """When any stored value last changed."""
        with self._uow:
            times = [s.updated_at for s in self._uow.platform.settings()]
        return max(times) if times else None
