"""Application settings loaded from ``SHIFTCODES_*`` environment variables."""

from __future__ import annotations

import ipaddress
from collections.abc import Mapping
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from shiftcodes.domain.errors import ValidationError
from shiftcodes.domain.resources import parse_cpus, parse_memory

DEFAULT_DATA_DIR = Path("/var/lib/shiftcodes")
DEFAULT_PUBLIC_ORIGIN = "https://monitoring.shiftcodes.net"
SESSION_IDLE_TIMEOUT = timedelta(hours=1)
SESSION_ABSOLUTE_TIMEOUT = timedelta(hours=12)
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class Settings(BaseSettings):
    """Runtime configuration. Contains paths and URLs only, never secret values."""

    model_config = SettingsConfigDict(env_prefix="SHIFTCODES_", extra="ignore")

    environment: Literal["development", "production"] = "development"
    data_dir: Path = DEFAULT_DATA_DIR
    database_url: str
    docker_host: str = "unix:///var/run/docker.sock"
    # Path or name of the docker CLI; production uses a non-default path.
    docker_binary: str = "docker"
    git_binary: str = "git"
    # Where a project repository ("owner/name") is cloned from with the https transport
    # (public repositories only; the ssh transport uses the deploy key below).
    git_url_template: str = "https://github.com/{repository}.git"
    # Deploy key (private key file, mode 0600) in the worker-only keys volume. Unset means
    # the ssh transport is unavailable. The pinned host keys ship with the image.
    git_ssh_key_file: Path | None = None
    git_ssh_known_hosts_file: Path = Path("/etc/shiftcodes/github_known_hosts")
    # Webhook secret shared with GitHub (file in the keys volume, read by the worker only).
    github_webhook_secret_file: Path | None = None
    # Report run status to GitHub; the token is the environment secret named below.
    github_status_enabled: bool = False
    github_status_token_secret: str = "GITHUB_STATUS_TOKEN"  # noqa: S105 - a secret *name*
    # Host-side path of ``<data_dir>/workspaces`` when the worker itself runs in a container
    # (bind mounts are resolved by the host daemon). ``None`` means the same path.
    workspace_host_dir: Path | None = None
    workspace_max_bytes: int = Field(default=2 * 1024**3, gt=0)
    step_log_max_bytes: int = Field(default=5 * 1024**2, gt=0)
    parallel_workers: int = Field(default=2, ge=1, le=16)
    max_concurrent_runs: int = Field(default=1, ge=1, le=16)
    # Upper bounds for step containers and image builds; a pipeline may only lower them.
    container_memory: str = "2g"
    container_cpus: str = "2"
    build_memory: str = "2g"
    # Default wall-clock limit of one container step when no ``timeout`` applies.
    step_timeout_seconds: int = Field(default=3600, gt=0)
    queue_poll_interval: float = Field(default=1.0, gt=0)
    cancel_poll_interval: float = Field(default=1.0, gt=0)
    sse_poll_interval: float = Field(default=0.5, gt=0)
    sse_heartbeat_interval: float = Field(default=15.0, gt=0)
    public_origin: str = DEFAULT_PUBLIC_ORIGIN
    worker_socket_path: Path = Path("/run/shiftcodes/worker.sock")
    log_level: LogLevel = "INFO"
    # Reverse proxies whose X-Forwarded-For is believed (IPs/CIDRs, comma separated in the
    # environment). Empty means nobody is trusted and the peer address is the client address.
    trusted_proxies: Annotated[list[str], NoDecode] = []
    secret_key_file: Path

    @field_validator("container_memory", "build_memory")
    @classmethod
    def _check_memory(cls, value: str) -> str:
        """Memory limits use Docker syntax (``512m``, ``2g``)."""
        try:
            parse_memory(value)
        except ValidationError as error:
            raise ValueError(str(error)) from error
        return value

    @field_validator("container_cpus")
    @classmethod
    def _check_cpus(cls, value: str) -> str:
        """CPU limits are positive decimal numbers."""
        try:
            parse_cpus(value)
        except ValidationError as error:
            raise ValueError(str(error)) from error
        return value

    @field_validator("trusted_proxies", mode="before")
    @classmethod
    def _parse_trusted_proxies(cls, value: object) -> object:
        """Accept a comma separated string; every entry must be an IP address or CIDR."""
        items = value.split(",") if isinstance(value, str) else value
        if not isinstance(items, list | tuple):
            return items
        cleaned = [str(item).strip() for item in items if str(item).strip()]
        for entry in cleaned:
            try:
                ipaddress.ip_network(entry, strict=False)
            except ValueError as error:
                raise ValueError(
                    f"trusted_proxies entry is not an IP or CIDR: {entry!r}"
                ) from error
        return cleaned

    @model_validator(mode="before")
    @classmethod
    def _derive_data_dir_defaults(cls, values: object) -> object:
        """Default the database and key file to locations under ``data_dir``."""
        if not isinstance(values, Mapping):
            return values
        merged = dict(values)
        data_dir = Path(merged.get("data_dir", DEFAULT_DATA_DIR))
        merged.setdefault("database_url", f"sqlite:///{data_dir / 'shiftcodes.db'}")
        merged.setdefault("secret_key_file", data_dir / "secret.key")
        return merged

    @model_validator(mode="after")
    def _validate_public_origin(self) -> Settings:
        """Require a bare origin; production additionally requires https."""
        parts = urlsplit(self.public_origin)
        allowed = {"https"} if self.environment == "production" else {"http", "https"}
        if parts.scheme not in allowed:
            raise ValueError(
                f"public_origin must use {' or '.join(sorted(allowed))} "
                f"in {self.environment}: {self.public_origin!r}"
            )
        if not parts.hostname:
            raise ValueError("public_origin must include a host")
        if parts.path not in ("", "/") or parts.query or parts.fragment or parts.username:
            raise ValueError("public_origin must be an origin without path, query or credentials")
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, loaded once from the environment."""
    return Settings()
