"""Parsing of Docker-style resource limits (``512m``, ``2g``, ``1.5``)."""

from __future__ import annotations

import re

from backend.domain.errors import ValidationError

_MEMORY = re.compile(r"([0-9]+)([bkmgBKMG]?)")
_CPUS = re.compile(r"[0-9]+(?:\.[0-9]+)?")
_UNITS = {"": 1, "b": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


def parse_memory(text: str) -> int:
    """Return the number of bytes of a Docker memory size; a bare number means bytes."""
    match = _MEMORY.fullmatch(text)
    if match is None:
        raise ValidationError(f"invalid memory size: {text!r} (use for example 512m or 2g)")
    size = int(match.group(1)) * _UNITS[match.group(2).lower()]
    if size <= 0:
        raise ValidationError(f"memory size must be greater than zero: {text!r}")
    return size


def parse_cpus(text: str) -> float:
    """Return the CPU count of a Docker ``--cpus`` value."""
    if _CPUS.fullmatch(text) is None:
        raise ValidationError(f"invalid cpu count: {text!r}")
    cpus = float(text)
    if cpus <= 0:
        raise ValidationError(f"cpu count must be greater than zero: {text!r}")
    return cpus
