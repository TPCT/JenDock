"""Structured JSON logging with secret redaction."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from backend.config import Settings

REDACTED = "[REDACTED]"
SENSITIVE_KEYS = ("password", "token", "secret", "authorization", "cookie", "api_key")
_STANDARD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class SecretRedactor(logging.Filter):
    """Masks registered secret values and sensitive keys in log records."""

    def __init__(self) -> None:
        super().__init__()
        self._secrets: set[str] = set()

    def register(self, secret: str) -> None:
        """Mask every future occurrence of ``secret``. Empty values are ignored."""
        if secret:
            self._secrets.add(secret)

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact the message and extra fields in place; never drops records."""
        record.msg = self.redact_text(record.getMessage())
        record.args = None
        for key in set(record.__dict__) - _STANDARD_ATTRS:
            record.__dict__[key] = self.redact_value(record.__dict__[key], key)
        return True

    def redact_text(self, text: str) -> str:
        """Replace registered secrets, longest first so overlaps mask fully."""
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, REDACTED)
        return text

    def redact_value(self, value: object, key: str = "") -> object:
        """Redact a value, recursing into mappings and sequences."""
        if _is_sensitive_key(key):
            return REDACTED
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            return {k: self.redact_value(v, str(k)) for k, v in value.items()}
        if isinstance(value, list | tuple):
            return [self.redact_value(item) for item in value]
        return value


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(marker in lowered for marker in SENSITIVE_KEYS)


class JsonFormatter(logging.Formatter):
    """Formats records as single-line JSON objects including ``extra`` fields.

    Exception and stack text are rendered here, after the filter ran, so they are
    redacted with the same redactor.
    """

    def __init__(self, redactor: SecretRedactor) -> None:
        super().__init__()
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        """Render ``record`` as JSON."""
        payload: dict[str, Any] = {  # Any: JSON values are heterogeneous
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self._redactor.redact_text(self.formatException(record.exc_info))
        if record.stack_info:
            payload["stack_info"] = self._redactor.redact_text(self.formatStack(record.stack_info))
        return json.dumps(payload, default=str)


def configure_logging(settings: Settings, stream: Any = None) -> SecretRedactor:
    """Configure the root logger for JSON output and return its redactor.

    ``stream`` defaults to stderr and can be injected for tests.
    """
    redactor = SecretRedactor()
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(JsonFormatter(redactor))
    handler.addFilter(redactor)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(settings.log_level)
    return redactor
