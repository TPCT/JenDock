"""The principal performing an action, recorded in the audit log."""

from __future__ import annotations

from dataclasses import dataclass

from shiftcodes.domain.enums import ActorType


@dataclass(frozen=True, slots=True)
class Actor:
    """Who did something: a user, an API token or the platform itself."""

    type: ActorType
    id: str | None = None

    @classmethod
    def system(cls, name: str | None = None) -> Actor:
        """Return the system actor, optionally naming the component."""
        return cls(ActorType.SYSTEM, name)
