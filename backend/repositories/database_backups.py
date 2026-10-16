"""Queries for backup records and their owning project/environment/connection."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend.repositories.models import DatabaseBackup, DatabaseConnection, Environment, Project


class DatabaseBackupRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, backup: DatabaseBackup) -> DatabaseBackup:
        self._session.add(backup)
        self._session.flush()
        return backup

    def get(self, backup_id: int) -> DatabaseBackup | None:
        return self._session.get(DatabaseBackup, backup_id)

    def has_running_for_connection(self, connection_id: int) -> bool:
        return (
            self._session.scalar(
                select(DatabaseBackup.id)
                .where(
                    DatabaseBackup.connection_id == connection_id,
                    DatabaseBackup.status == "running",
                )
                .limit(1)
            )
            is not None
        )

    def interrupted(self) -> list[DatabaseBackup]:
        rows = self._session.scalars(
            select(DatabaseBackup).where(DatabaseBackup.status == "running")
        ).all()
        return list(rows)

    def scheduled_on(self, local_start: datetime, local_end: datetime) -> bool:
        return (
            self._session.scalar(
                select(DatabaseBackup.id)
                .where(
                    DatabaseBackup.trigger == "schedule",
                    DatabaseBackup.started_at >= local_start,
                    DatabaseBackup.started_at < local_end,
                )
                .limit(1)
            )
            is not None
        )

    def list(
        self, slug: str | None = None, env: str | None = None
    ) -> Sequence[tuple[DatabaseBackup, str, str, str]]:
        query = (
            select(DatabaseBackup, Project.slug, Environment.name, DatabaseConnection.name)
            .join(Environment, Environment.id == DatabaseBackup.environment_id)
            .join(Project, Project.id == Environment.project_id)
            .outerjoin(DatabaseConnection, DatabaseConnection.id == DatabaseBackup.connection_id)
        )
        if slug is not None:
            query = query.where(Project.slug == slug)
        if env is not None:
            query = query.where(Environment.name == env)
        return self._session.execute(query.order_by(DatabaseBackup.started_at.desc())).all()

    def eligible(self) -> Sequence[tuple[Project, Environment, DatabaseConnection]]:
        query = (
            select(Project, Environment, DatabaseConnection)
            .join(Environment, Environment.project_id == Project.id)
            .join(DatabaseConnection, DatabaseConnection.environment_id == Environment.id)
            .where((Environment.name == "production") | Environment.include_in_backups.is_(True))
        )
        return self._session.execute(
            query.order_by(Project.slug, Environment.name, DatabaseConnection.name)
        ).all()

    def delete_for_connection(self, connection_id: int) -> None:
        self._session.execute(
            delete(DatabaseBackup).where(DatabaseBackup.connection_id == connection_id)
        )
