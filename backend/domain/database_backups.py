"""Values and pure rules for database backups."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

BackupTrigger = Literal["schedule", "manual"]
BackupStatus = Literal["running", "succeeded", "failed"]
CAIRO = ZoneInfo("Africa/Cairo")


class BackupError(Exception):
    """A safe, actionable backup failure message that contains no secret material."""


@dataclass(frozen=True, slots=True)
class BackupSettings:
    access_key_id: str = ""
    bucket: str = "aic-consultation"
    region: str = "eu-west-3"
    prefix: str = "backups"
    hour: int = 3
    enabled: bool = False
    mysql_image: str = "mysql:8.4"
    mariadb_image: str = "mariadb:11.4"
    postgres_image: str = "postgres:17"
    secret_configured: bool = False


def next_run(now: datetime, hour: int) -> datetime:
    """Return the next configured Cairo wall-clock hour, as an aware UTC instant."""
    local = now.astimezone(CAIRO)
    if not 0 <= hour <= 23:
        raise ValueError("schedule hour must be from 0 to 23")
    candidate = datetime.combine(local.date(), time(hour, tzinfo=CAIRO))
    if candidate <= local:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def connection_prefix(prefix: str, project: str, environment: str, connection: str) -> str:
    return "/".join(part.strip("/") for part in (prefix, project, environment, connection)) + "/"


def object_key(  # noqa: PLR0913, PLR0917 - each key component is a separate path segment
    prefix: str,
    project: str,
    environment: str,
    connection: str,
    run_time: datetime,
    postgres: bool,
) -> str:
    suffix = ".dump" if postgres else ".sql.gz"
    local = run_time.astimezone(CAIRO)
    timestamp = local.strftime("%Y-%m-%dT%H%M%S")
    return f"{connection_prefix(prefix, project, environment, connection)}{timestamp}{suffix}"
