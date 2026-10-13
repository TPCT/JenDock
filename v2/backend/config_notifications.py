"""Notification settings loaded from ``JENDOCK_NOTIFY_*`` environment variables.

Kept apart from the core settings so notifications stay self-contained. Paths and numbers
only; channel credentials live encrypted in the database (see ``docs/api-contract.md``).
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class NotificationSettings(BaseSettings):
    """Runtime configuration of the notification outbox and monitors."""

    model_config = SettingsConfigDict(env_prefix="JENDOCK_NOTIFY_", extra="ignore")

    enabled: bool = True
    # Same event and resource within this many minutes collapse into one message per channel.
    dedupe_minutes: int = Field(default=30, ge=0, le=1440)
    max_attempts: int = Field(default=7, ge=1, le=20)
    # First retry after this many seconds; every further one doubles it (capped at one hour).
    retry_base_seconds: float = Field(default=30.0, gt=0)
    send_timeout_seconds: float = Field(default=10.0, gt=0)
    poll_interval_seconds: float = Field(default=2.0, gt=0)
    history_days: int = Field(default=30, ge=1, le=365)
    disk_threshold_percent: int = Field(default=90, ge=50, le=99)
    disk_check_seconds: float = Field(default=300.0, gt=0)
    # More than this many invalid webhook signatures within the window is a spike.
    webhook_failure_threshold: int = Field(default=20, ge=1)
    webhook_failure_window_minutes: int = Field(default=10, ge=1, le=1440)


@lru_cache
def get_notification_settings() -> NotificationSettings:
    """The process-wide notification settings."""
    return NotificationSettings()
