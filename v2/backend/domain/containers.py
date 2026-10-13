"""Container management value types and the validation rules shared by API and worker."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from backend.domain.enums import EnvironmentName
from backend.domain.errors import ValidationError

COMPOSE_PROJECT_LABEL = "com.docker.compose.project"
COMPOSE_SERVICE_LABEL = "com.docker.compose.service"
OCI_REVISION_LABEL = "org.opencontainers.image.revision"
COMMIT_LABEL = "jendock.commit"
MAX_PATH_LENGTH = 1024
MAX_ENV_VALUE_BYTES = 8192
MAX_ENV_CHANGES = 100
# File names that usually hold credentials; the browser refuses them so that it cannot be
# used to read around the masking of environment values.
_PROTECTED_NAME = re.compile(
    r"(^\.env($|\.)|^\.(ssh|aws|azure|kube|docker|gnupg|netrc|npmrc|pypirc)$"
    r"|^id_(rsa|dsa|ecdsa|ed25519)|\.(pem|key|p12|pfx|jks|keystore)$"
    r"|^(shadow|gshadow|credentials|secrets?)(\.|$))",
    re.IGNORECASE,
)
DENIED_ROOTS = ("/proc", "/sys", "/dev")
_CONTAINER_ID = re.compile(r"[a-f0-9]{64}")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_SENSITIVE_NAME = re.compile(
    r"pass(word|wd)?|secret|token|api[_-]?key|access[_-]?key|credential|private[_-]?key|auth",
    re.IGNORECASE,
)


class LifecycleAction(StrEnum):
    """What a user may ask of a container."""

    START = "start"
    STOP = "stop"
    RESTART = "restart"


class ContainerState(StrEnum):
    """Docker's container states."""

    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    RESTARTING = "restarting"
    EXITED = "exited"
    REMOVING = "removing"
    DEAD = "dead"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class EnvironmentKey:
    """Which environment of which project; also the key of the per-environment lock."""

    project: str
    environment: EnvironmentName

    def __str__(self) -> str:
        return f"{self.project}/{self.environment.value}"


@dataclass(frozen=True, slots=True)
class PortBinding:
    """A container port and where (if anywhere) it is published on the host."""

    container_port: str
    host_ip: str | None = None
    host_port: str | None = None


@dataclass(frozen=True, slots=True)
class MountInfo:
    """One mount of a container (never the content, only where it points)."""

    type: str
    source: str
    destination: str
    read_only: bool


@dataclass(frozen=True, slots=True)
class ContainerInfo:
    """What the inventory shows about one container. Environment values are not included."""

    id: str
    name: str
    service: str | None
    image: str
    image_id: str
    revision: str | None
    commit: str | None
    state: str
    health: str | None
    restart_count: int
    started_at: datetime | None
    ports: tuple[PortBinding, ...] = ()
    mounts: tuple[MountInfo, ...] = ()
    cpus: float | None = None
    memory_bytes: int | None = None
    exit_code: int | None = None

    @property
    def running(self) -> bool:
        """Whether the container is running (or restarting itself)."""
        return self.state in {ContainerState.RUNNING.value, ContainerState.RESTARTING.value}


@dataclass(frozen=True, slots=True)
class EnvVarInfo:
    """One variable as listed: the value is never part of it."""

    name: str
    sensitive: bool
    overridden: bool


@dataclass(frozen=True, slots=True)
class LimitChange:
    """A requested CPU/memory limit; ``None`` leaves that limit as it is."""

    cpus: float | None = None
    memory: str | None = None


@dataclass(frozen=True, slots=True)
class EnvChange:
    """Variables to set and override names to drop (reverting to the compose file's value)."""

    set: dict[str, str] = field(default_factory=dict)
    remove: tuple[str, ...] = ()


def validate_container_id(value: str) -> str:
    """Require the full 64 hex character container id (names and prefixes are not identities)."""
    if _CONTAINER_ID.fullmatch(value) is None:
        raise ValidationError("container id must be the full 64 character id")
    return value


def validate_env_name(name: str) -> str:
    """Environment variable names: letters, digits and underscore, not starting with a digit."""
    if _ENV_NAME.fullmatch(name) is None:
        raise ValidationError(f"invalid environment variable name: {name!r}")
    return name


def validate_env_change(change: EnvChange) -> EnvChange:
    """Check names and values of an edit; values may not contain NUL or exceed 8 KiB."""
    if not change.set and not change.remove:
        raise ValidationError("nothing to change")
    if len(change.set) + len(change.remove) > MAX_ENV_CHANGES:
        raise ValidationError(f"at most {MAX_ENV_CHANGES} changes at once")
    for name, value in change.set.items():
        validate_env_name(name)
        if "\0" in value or len(value.encode()) > MAX_ENV_VALUE_BYTES:
            raise ValidationError(f"invalid value for {name}: NUL or more than 8 KiB")
    for name in change.remove:
        validate_env_name(name)
        if name in change.set:
            raise ValidationError(f"{name} is both set and removed")
    return change


def is_sensitive_name(name: str) -> bool:
    """Whether a variable name suggests a credential (its value is masked in log output)."""
    return _SENSITIVE_NAME.search(name) is not None


def validate_container_path(path: str) -> str:
    """Return a clean absolute path inside a container or raise ``ValidationError``.

    No ``..`` or ``.`` parts, no empty parts, no control characters, no pseudo file systems and
    no file names that usually hold credentials. The path is only ever passed as one argv
    element, never interpolated into a shell command.
    """
    if not path.startswith("/") or len(path) > MAX_PATH_LENGTH:
        raise ValidationError("path must be absolute and at most 1024 characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise ValidationError("path contains control characters")
    parts = path.split("/")[1:]
    if path != "/" and (any(part in ("", ".", "..") for part in parts)):
        raise ValidationError("path must be normalized: no '..', '.', or empty parts")
    if any(path == root or path.startswith(root + "/") for root in DENIED_ROOTS):
        raise ValidationError("this part of the file system cannot be browsed")
    if any(_PROTECTED_NAME.search(part) for part in parts):
        raise ValidationError("this file is protected and cannot be shown")
    return path


def is_protected_name(name: str) -> bool:
    """Whether a directory entry is hidden from listings (credential-like file names)."""
    return _PROTECTED_NAME.search(name) is not None
