"""Ingress settings loaded from ``JENDOCK_INGRESS_*`` environment variables.

Kept apart from the core settings so the API and the worker can load them independently.
Contains paths, URLs and addresses only; the Cloudflare token lives in the secret store.
"""

from __future__ import annotations

import ipaddress
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_HELPER_SOCKET = Path("/run/jendock-ingress/host.sock")
LETS_ENCRYPT_DIRECTORY = "https://acme-v02.api.letsencrypt.org/directory"
CLOUDFLARE_API = "https://api.cloudflare.com/client/v4"
AcmeChallenge = Literal["dns-01", "http-01"]


class IngressSettings(BaseSettings):
    """Runtime configuration of domains, DNS, Apache and TLS."""

    model_config = SettingsConfigDict(env_prefix="JENDOCK_INGRESS_", extra="ignore")

    enabled: bool = False
    helper_socket_path: Path = DEFAULT_HELPER_SOCKET
    # The server's public addresses; used when a domain does not name its own DNS target.
    public_ipv4: str | None = None
    public_ipv6: str | None = None
    allow_wildcards: bool = False
    cloudflare_api_base: str = CLOUDFLARE_API
    acme_directory_url: str = LETS_ENCRYPT_DIRECTORY
    acme_email: str | None = None
    # DNS-01 is primary (see docs/ingress.md); HTTP-01 stays available through the helper.
    acme_challenge: AcmeChallenge = "dns-01"
    # CA bundle to trust for the ACME directory (only needed for a private CA such as Pebble).
    acme_ca_bundle: Path | None = None
    # Defaults to ``<data_dir>/worker-private/acme-account.key`` when unset.
    acme_account_key_file: Path | None = None
    dns_propagation_wait_seconds: float = Field(default=5.0, ge=0)
    acme_poll_timeout_seconds: float = Field(default=180.0, gt=0)
    renew_before_days: int = Field(default=30, ge=1, le=60)
    renew_retry_hours: int = Field(default=6, ge=1, le=72)
    renewal_interval_seconds: float = Field(default=3600.0, gt=0)
    check_timeout_seconds: float = Field(default=10.0, gt=0)

    @field_validator("public_ipv4", "public_ipv6", "acme_email", mode="before")
    @classmethod
    def _empty_means_unset(cls, value: object) -> object:
        """An empty value (an unset compose variable) means "not configured": the stored
        platform setting, or the domain's own DNS target, applies instead."""
        return None if isinstance(value, str) and value.strip() == "" else value

    @field_validator("public_ipv4")
    @classmethod
    def _check_ipv4(cls, value: str | None) -> str | None:
        if value is not None:
            ipaddress.IPv4Address(value)
        return value

    @field_validator("public_ipv6")
    @classmethod
    def _check_ipv6(cls, value: str | None) -> str | None:
        if value is not None:
            ipaddress.IPv6Address(value)
        return value


@lru_cache
def get_ingress_settings() -> IngressSettings:
    """Return the process-wide ingress settings."""
    return IngressSettings()
