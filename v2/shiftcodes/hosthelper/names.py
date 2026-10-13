"""Strict validation of everything the helper writes into files or paths.

The helper does not trust its caller: hostnames, ports, tokens and file names are checked
here with allowlist patterns, so no value can contain whitespace, quotes, newlines, ``/`` or
``..``. Hostnames must already be lowercase ASCII (IDNA A-labels).
"""

from __future__ import annotations

import re

from shiftcodes.hosthelper.errors import InvalidRequestError

MAX_HOSTNAME_LENGTH = 253
WILDCARD_PREFIX = "*."
WILDCARD_FILE_PREFIX = "_wildcard_."
_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_ALPHA_TLD = re.compile(r"[a-z][a-z0-9-]*")
_ACME_TOKEN = re.compile(r"[A-Za-z0-9_-]{16,128}")
_ACME_CONTENT = re.compile(r"[A-Za-z0-9_.-]{16,256}")
MIN_PORT = 1
MAX_PORT = 65535


def is_valid_ascii_hostname(value: str, *, allow_wildcard: bool = False) -> bool:
    """Whether ``value`` is a lowercase ASCII DNS name with at least two labels."""
    if not value.isascii() or len(value) > MAX_HOSTNAME_LENGTH:
        return False
    name = value
    if name.startswith(WILDCARD_PREFIX):
        if not allow_wildcard:
            return False
        name = name[len(WILDCARD_PREFIX) :]
    labels = name.split(".")
    if len(labels) < 2 or not all(_LABEL.fullmatch(label) for label in labels):
        return False
    return _ALPHA_TLD.fullmatch(labels[-1]) is not None


def require_hostname(value: object, *, allow_wildcard: bool) -> str:
    """Return ``value`` if it is a valid hostname, else raise ``InvalidRequestError``."""
    if not isinstance(value, str) or not is_valid_ascii_hostname(
        value, allow_wildcard=allow_wildcard
    ):
        raise InvalidRequestError("invalid hostname")
    return value


def hostname_matches_suffixes(hostname: str, suffixes: tuple[str, ...]) -> bool:
    """Whether ``hostname`` is a configured zone or below one."""
    bare = hostname.removeprefix(WILDCARD_PREFIX)
    return any(bare == suffix or bare.endswith("." + suffix) for suffix in suffixes)


def file_stem(hostname: str) -> str:
    """The file-system safe name of a hostname (wildcards get a fixed prefix)."""
    if hostname.startswith(WILDCARD_PREFIX):
        return WILDCARD_FILE_PREFIX + hostname[len(WILDCARD_PREFIX) :]
    return hostname


def require_port(value: object) -> int:
    """Return ``value`` as an int port (booleans and strings are rejected)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidRequestError("port must be an integer")
    if not MIN_PORT <= value <= MAX_PORT:
        raise InvalidRequestError("port out of range")
    return value


def require_acme_token(value: object) -> str:
    """Validate an ACME HTTP-01 token (base64url characters only, so it is a safe file name)."""
    if not isinstance(value, str) or _ACME_TOKEN.fullmatch(value) is None:
        raise InvalidRequestError("invalid challenge token")
    return value


def require_acme_content(value: object) -> str:
    """Validate the key authorization served for an HTTP-01 challenge."""
    if not isinstance(value, str) or _ACME_CONTENT.fullmatch(value) is None:
        raise InvalidRequestError("invalid challenge content")
    return value
