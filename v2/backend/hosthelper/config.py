"""Administrator-owned configuration of the host helper (a JSON file, never request input)."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from backend.hosthelper.errors import HelperError
from backend.hosthelper.names import is_valid_ascii_hostname

KNOWN_KEYS = frozenset(
    {
        "socket_path",
        "socket_mode",
        "socket_group",
        "allowed_uids",
        "vhost_dir",
        "cert_dir",
        "webroot_dir",
        "apachectl",
        "allowed_hostname_suffixes",
        "allowed_upstream_ports",
        "allow_wildcards",
        "max_vhosts",
        "command_timeout_seconds",
    }
)
REQUIRED_KEYS = frozenset(
    {
        "socket_path",
        "allowed_uids",
        "vhost_dir",
        "cert_dir",
        "webroot_dir",
        "apachectl",
        "allowed_hostname_suffixes",
        "allowed_upstream_ports",
    }
)
DEFAULT_SOCKET_MODE = 0o660
DEFAULT_MAX_VHOSTS = 200
DEFAULT_COMMAND_TIMEOUT = 30.0
MAX_COMMAND_TIMEOUT = 300.0
LAST_PORT = 65535


class ConfigError(HelperError):
    """The helper configuration is unusable; the helper refuses to start."""

    def __init__(self, message: str) -> None:
        super().__init__("configuration", message)


@dataclass(frozen=True, slots=True)
class PortRange:
    """An inclusive range of host ports Apache may proxy to."""

    first: int
    last: int

    def contains(self, port: int) -> bool:
        """Whether ``port`` lies in the range."""
        return self.first <= port <= self.last


@dataclass(frozen=True, slots=True)
class HelperConfig:
    """Everything the helper may touch; all paths are absolute and fixed by the administrator."""

    socket_path: Path
    socket_mode: int
    socket_group: str | None
    allowed_uids: frozenset[int]
    vhost_dir: Path
    cert_dir: Path
    webroot_dir: Path
    apachectl: tuple[str, ...]
    allowed_hostname_suffixes: tuple[str, ...]
    allowed_upstream_ports: tuple[PortRange, ...]
    allow_wildcards: bool = False
    max_vhosts: int = DEFAULT_MAX_VHOSTS
    command_timeout_seconds: float = DEFAULT_COMMAND_TIMEOUT

    def port_allowed(self, port: int) -> bool:
        """Whether Apache may proxy to ``port``."""
        return any(item.contains(port) for item in self.allowed_upstream_ports)


def load_config(path: Path, *, check_owner: bool = True) -> HelperConfig:
    """Read and validate the config file; refuse files others could have modified."""
    if check_owner:
        _check_file_safety(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ConfigError(f"cannot read configuration {path}: {error}") from error
    return parse_config(raw)


def _check_file_safety(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError as error:
        raise ConfigError(f"cannot stat configuration {path}: {error}") from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise ConfigError("configuration must be a regular file, not a symlink")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise ConfigError("configuration must not be group or world writable")
    if info.st_uid != os.geteuid():
        raise ConfigError("configuration must be owned by the user running the helper")


def parse_config(raw: object) -> HelperConfig:
    """Validate the decoded JSON document."""
    if not isinstance(raw, dict):
        raise ConfigError("configuration must be a JSON object")
    unknown = set(raw) - KNOWN_KEYS
    missing = REQUIRED_KEYS - set(raw)
    if unknown or missing:
        raise ConfigError(f"unknown keys {sorted(unknown)} / missing keys {sorted(missing)}")
    return HelperConfig(
        socket_path=_abs_path(raw["socket_path"], "socket_path"),
        socket_mode=_socket_mode(raw.get("socket_mode", "0660")),
        socket_group=_optional_str(raw.get("socket_group"), "socket_group"),
        allowed_uids=_uids(raw["allowed_uids"]),
        vhost_dir=_abs_path(raw["vhost_dir"], "vhost_dir"),
        cert_dir=_abs_path(raw["cert_dir"], "cert_dir"),
        webroot_dir=_abs_path(raw["webroot_dir"], "webroot_dir"),
        apachectl=_argv(raw["apachectl"]),
        allowed_hostname_suffixes=_suffixes(raw["allowed_hostname_suffixes"]),
        allowed_upstream_ports=_ranges(raw["allowed_upstream_ports"]),
        allow_wildcards=_boolean(raw.get("allow_wildcards", False), "allow_wildcards"),
        max_vhosts=_bounded_int(raw.get("max_vhosts", DEFAULT_MAX_VHOSTS), "max_vhosts", 1, 5000),
        command_timeout_seconds=_timeout(raw.get("command_timeout_seconds")),
    )


def _abs_path(value: object, name: str) -> Path:
    if not isinstance(value, str) or "\0" in value:
        raise ConfigError(f"{name} must be a path string")
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise ConfigError(f"{name} must be an absolute path without '..'")
    return path


def _optional_str(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{name} must be a non-empty string")
    return value


def _socket_mode(value: object) -> int:
    try:
        mode = int(str(value), 8)
    except ValueError as error:
        raise ConfigError("socket_mode must be an octal string such as 0660") from error
    if mode & 0o007 or mode & ~0o777:
        raise ConfigError("socket_mode must not grant access to others")
    return mode


def _uids(value: object) -> frozenset[int]:
    if not isinstance(value, list) or not value:
        raise ConfigError("allowed_uids must be a non-empty list")
    if not all(isinstance(uid, int) and not isinstance(uid, bool) and uid >= 0 for uid in value):
        raise ConfigError("allowed_uids must contain non-negative integers")
    return frozenset(value)


def _argv(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(a, str) for a in value):
        raise ConfigError("apachectl must be a non-empty argv list of strings")
    if not Path(value[0]).is_absolute():
        raise ConfigError("apachectl[0] must be an absolute path")
    return tuple(value)


def _suffixes(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError("allowed_hostname_suffixes must be a non-empty list")
    for item in value:
        if not isinstance(item, str) or not is_valid_ascii_hostname(item):
            raise ConfigError(f"invalid hostname suffix: {item!r}")
    return tuple(value)


def _ranges(value: object) -> tuple[PortRange, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError("allowed_upstream_ports must be a non-empty list of [first, last]")
    ranges = []
    for item in value:
        ok = (
            isinstance(item, list)
            and len(item) == 2
            and all(isinstance(p, int) and not isinstance(p, bool) for p in item)
            and 1024 <= item[0] <= item[1] <= LAST_PORT
        )
        if not ok:
            raise ConfigError(f"invalid port range (1024-65535, first <= last): {item!r}")
        ranges.append(PortRange(item[0], item[1]))
    return tuple(ranges)


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be true or false")
    return value


def _bounded_int(value: object, name: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"{name} must be an integer between {low} and {high}")
    return value


def _timeout(value: object) -> float:
    if value is None:
        return DEFAULT_COMMAND_TIMEOUT
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError("command_timeout_seconds must be a number")
    if not 1 <= value <= MAX_COMMAND_TIMEOUT:
        raise ConfigError(f"command_timeout_seconds must be 1-{MAX_COMMAND_TIMEOUT:.0f}")
    return float(value)
