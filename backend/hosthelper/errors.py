"""Errors of the host helper; the code is part of the wire protocol."""

from __future__ import annotations


class HelperError(Exception):
    """A request was refused or an operation failed; ``code`` is machine readable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class InvalidRequestError(HelperError):
    """The request is malformed or violates the allowlist."""

    def __init__(self, message: str) -> None:
        super().__init__("invalid_request", message)
