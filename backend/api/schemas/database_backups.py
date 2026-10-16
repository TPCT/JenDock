"""Wire shapes for backup settings and run records."""

from __future__ import annotations

import json
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from backend.domain.database_backups import BackupSettings
from backend.repositories.models import DatabaseBackup
from backend.services.database_backups import BackupView


class BackupSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    access_key_id: str = Field(max_length=128)
    secret_key: str | None = Field(default=None, max_length=512, repr=False)
    bucket: str = Field(default="aic-consultation", max_length=63)
    region: str = Field(default="eu-west-3", max_length=64)
    prefix: str = Field(default="backups", max_length=180)
    hour: int = Field(default=3, ge=0, le=23)
    enabled: bool = False
    mysql_image: str = Field(default="mysql:8.4", max_length=255)
    mariadb_image: str = Field(default="mariadb:11.4", max_length=255)
    postgres_image: str = Field(default="postgres:17", max_length=255)

    def to_settings(self) -> BackupSettings:
        return BackupSettings(
            access_key_id=self.access_key_id,
            bucket=self.bucket,
            region=self.region,
            prefix=self.prefix,
            hour=self.hour,
            enabled=self.enabled,
            mysql_image=self.mysql_image,
            mariadb_image=self.mariadb_image,
            postgres_image=self.postgres_image,
        )


class BackupSettingsResponse(BaseModel):
    access_key_id: str
    bucket: str
    region: str
    prefix: str
    hour: int
    enabled: bool
    mysql_image: str
    mariadb_image: str
    postgres_image: str
    secret_configured: bool
    iam_policy: str

    @classmethod
    def from_settings(cls, settings: BackupSettings) -> BackupSettingsResponse:
        prefix = settings.prefix.strip("/")
        resource_prefix = f"{prefix}/*" if prefix else "*"
        policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "s3:PutObject",
                        "s3:GetObject",
                        "s3:DeleteObject",
                        "s3:AbortMultipartUpload",
                        "s3:ListMultipartUploadParts",
                    ],
                    "Resource": f"arn:aws:s3:::{settings.bucket}/{resource_prefix}",
                },
                {
                    "Effect": "Allow",
                    "Action": "s3:ListBucket",
                    "Resource": f"arn:aws:s3:::{settings.bucket}",
                    "Condition": {"StringLike": {"s3:prefix": f"{prefix}/*" if prefix else "*"}},
                },
            ],
        }
        return cls(
            **{
                "access_key_id": settings.access_key_id,
                "bucket": settings.bucket,
                "region": settings.region,
                "prefix": settings.prefix,
                "hour": settings.hour,
                "enabled": settings.enabled,
                "mysql_image": settings.mysql_image,
                "mariadb_image": settings.mariadb_image,
                "postgres_image": settings.postgres_image,
                "secret_configured": settings.secret_configured,
                "iam_policy": json.dumps(policy, indent=2),
            }
        )


class BackupResponse(BaseModel):
    id: int
    project: str
    environment: str
    connection: str
    trigger: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    size_bytes: int | None
    object_key: str | None
    error: str | None

    @classmethod
    def from_model(
        cls, row: DatabaseBackup, project: str, environment: str, connection: str
    ) -> BackupResponse:
        return cls(
            id=row.id,
            project=project,
            environment=environment,
            connection=connection,
            trigger=row.trigger,
            status=row.status,
            started_at=row.started_at,
            finished_at=row.finished_at,
            size_bytes=row.size_bytes,
            object_key=row.object_key,
            error=row.error,
        )

    @classmethod
    def from_view(cls, view: BackupView) -> BackupResponse:
        return cls.from_model(view.record, view.project, view.environment, view.connection)
