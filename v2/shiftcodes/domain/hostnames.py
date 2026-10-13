"""Validated public hostnames (IDNA, no wildcards unless explicitly allowed)."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

import idna

from shiftcodes.domain.errors import ValidationError
from shiftcodes.hosthelper.names import WILDCARD_PREFIX, is_valid_ascii_hostname

RESERVED_SUFFIXES = (".local", ".localhost", ".internal", ".arpa", ".invalid", ".onion")
RESERVED_NAMES = frozenset({"localhost"})


@dataclass(frozen=True, slots=True)
class Hostname:
    """A canonical hostname: lowercase ASCII, internationalized labels as ``xn--`` A-labels."""

    value: str

    def __post_init__(self) -> None:
        # Direct construction accepts only an already canonical value.
        wildcard = self.value.startswith(WILDCARD_PREFIX)
        if not is_valid_ascii_hostname(self.value, allow_wildcard=wildcard) or _reserved(
            self.value
        ):
            raise ValidationError(f"invalid hostname: {self.value!r}")

    @classmethod
    def parse(cls, raw: str, *, allow_wildcard: bool = False) -> Hostname:
        """Normalize and validate user input.

        Unicode names are converted with IDNA 2008 (UTS #46 mapping); one trailing dot is
        dropped; IP literals, single labels, reserved zones and (unless ``allow_wildcard``)
        ``*.`` names are rejected.
        """
        text = raw.strip().lower().removesuffix(".")
        wildcard = text.startswith(WILDCARD_PREFIX)
        if wildcard and not allow_wildcard:
            raise ValidationError("wildcard hostnames are not allowed")
        body = text.removeprefix(WILDCARD_PREFIX) if wildcard else text
        if not body or _is_ip(body):
            raise ValidationError(f"invalid hostname: {raw!r}")
        try:
            ascii_body = idna.encode(body, uts46=True, std3_rules=True).decode("ascii")
        except (idna.IDNAError, UnicodeError) as error:
            raise ValidationError(f"invalid hostname: {raw!r}") from error
        return cls(WILDCARD_PREFIX + ascii_body if wildcard else ascii_body)

    @property
    def is_wildcard(self) -> bool:
        """Whether this is a ``*.zone`` name."""
        return self.value.startswith(WILDCARD_PREFIX)

    def __str__(self) -> str:
        return self.value


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
    except ValueError:
        return False
    return True


def _reserved(value: str) -> bool:
    bare = value.removeprefix(WILDCARD_PREFIX)
    return bare in RESERVED_NAMES or bare.endswith(RESERVED_SUFFIXES)
