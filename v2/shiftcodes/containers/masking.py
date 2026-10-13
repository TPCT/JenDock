"""Masking of secrets in container output.

Two layers: the values of the container's own credential-like environment variables are
replaced wherever they appear, and common ``key=value`` / bearer-token shapes are masked even
when the value is unknown to the platform.
"""

from __future__ import annotations

import re
from collections.abc import Mapping

from shiftcodes.domain.containers import is_sensitive_name

MASK = "[REDACTED]"
MIN_SECRET_LENGTH = 4
_KEY_VALUE = re.compile(
    r"""(?ix)
    (\b[\w.-]*(?:pass(?:word|wd)?|secret|token|api[_-]?key|access[_-]?key|private[_-]?key
    |authorization|credential)[\w.-]*["']?\s*[:=]\s*["']?)
    (?!\[REDACTED\])
    (?:bearer\s+|basic\s+)?
    ([^\s"',;&]+)
    """
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*")


class LogMasker:
    """Masks secrets in one container's output."""

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        values = {
            value
            for name, value in (environment or {}).items()
            if is_sensitive_name(name) and len(value) >= MIN_SECRET_LENGTH
        }
        self._values = sorted(values, key=len, reverse=True)

    def mask(self, line: str) -> str:
        """The line with known secret values and secret-shaped fragments replaced."""
        for value in self._values:
            line = line.replace(value, MASK)
        line = _PRIVATE_KEY.sub(f"{MASK} (private key)", line)
        line = _BEARER.sub(lambda match: f"{match.group(1)} {MASK}", line)
        return _KEY_VALUE.sub(lambda match: f"{match.group(1)}{MASK}", line)
