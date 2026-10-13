"""Value rules of database connections (the inspector's per-environment configuration)."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

from shiftcodes.domain.enums import EnvironmentName
from shiftcodes.domain.errors import ValidationError
from shiftcodes.inspector.model import Engine as DatabaseEngine
from shiftcodes.inspector.model import TlsMode

__all__ = [
    "DEFAULT_PORTS",
    "MAX_CONNECTIONS_PER_ENVIRONMENT",
    "ConnectionRef",
    "DatabaseEngine",
    "TableRef",
    "TlsMode",
    "password_secret_name",
    "validate_connection_name",
    "validate_database_name",
    "validate_host",
    "validate_password_value",
    "validate_port",
    "validate_username",
]

DEFAULT_PORTS = {
    DatabaseEngine.POSTGRESQL: 5432,
    DatabaseEngine.MYSQL: 3306,
    DatabaseEngine.MARIADB: 3306,
}
MAX_CONNECTIONS_PER_ENVIRONMENT = 10
MAX_PASSWORD_BYTES = 1024
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
_HOSTNAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,251}[A-Za-z0-9])?")
_FORBIDDEN_HOSTS = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_IDENTIFIER_LIMIT = 128


@dataclass(frozen=True, slots=True)
class ConnectionRef:
    """Identifies one configured connection: project slug, environment and connection name."""

    slug: str
    env: EnvironmentName
    name: str


@dataclass(frozen=True, slots=True)
class TableRef:
    """Identifies a table of a connection."""

    connection: ConnectionRef
    schema: str
    table: str


def validate_connection_name(name: str) -> str:
    """Lowercase letters, digits and ``-``; 1-48 characters (it names the password secret)."""
    if not _NAME.fullmatch(name):
        raise ValidationError(
            "connection name must be 1-48 characters of lowercase letters, digits or '-'"
        )
    return name


def password_secret_name(connection_name: str) -> str:
    """Name of the secret that holds the password (uses the environment secret store)."""
    return "DBCONN_" + connection_name.upper().replace("-", "_") + "_PASSWORD"


def validate_host(host: str) -> str:
    """A compose service name or other DNS name or IP of a service in the environment network.

    Loopback, unspecified and link-local addresses are refused outright; the worker enforces
    the real boundary (the environment's network) when it connects.
    """
    if host.lower() in _FORBIDDEN_HOSTS:
        raise ValidationError("host must be a service of the environment network, not localhost")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not _HOSTNAME.fullmatch(host):
            raise ValidationError(f"invalid host: {host!r}") from None
        return host
    if address.is_loopback or address.is_unspecified or address.is_link_local:
        raise ValidationError("host must not be a loopback, unspecified or link-local address")
    return str(address)


def validate_port(port: int) -> int:
    """TCP port 1-65535."""
    if not 1 <= port <= 65535:
        raise ValidationError("port must be between 1 and 65535")
    return port


def _identifier(value: str, label: str) -> str:
    if not value or len(value) > _IDENTIFIER_LIMIT or _CONTROL.search(value):
        raise ValidationError(f"{label} must be 1-{_IDENTIFIER_LIMIT} printable characters")
    return value


def validate_database_name(name: str) -> str:
    """Database name as the server knows it."""
    return _identifier(name, "database")


def validate_username(name: str) -> str:
    """Login name on the database server."""
    return _identifier(name, "username")


def validate_password_value(password: str) -> str:
    """Database passwords are not length-policed (the server decides); only bounded."""
    if not password or len(password.encode()) > MAX_PASSWORD_BYTES or "\0" in password:
        raise ValidationError(f"password must be 1-{MAX_PASSWORD_BYTES} bytes without NUL")
    return password
