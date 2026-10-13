"""SQLAlchemy ORM models for the platform's own database.

ON DELETE policy: rows that are owned by a parent and meaningless without it
(environments, pipeline versions, secrets, stages, steps, login sessions) use CASCADE.
Run history references its environment with RESTRICT so history is never lost silently;
projects are archived, not deleted. Optional references to users (``created_by``,
``triggered_by``) and to API tokens (``triggered_by_token_id``) use SET NULL so removing
them keeps the history. API tokens belong to the user that created them and are CASCADE
deleted with that user, so a token never outlives its owner.
All timestamps are timezone-aware UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import (
    JSON,
    DateTime,
    Dialect,
    Enum,
    ForeignKey,
    Index,
    LargeBinary,
    MetaData,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from shiftcodes.domain.clock import utcnow
from shiftcodes.domain.enums import (
    ActorType,
    EnvironmentName,
    PipelineSource,
    RunStatus,
    RunTrigger,
)

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
ENUM_LENGTH = 16


class UtcDateTime(TypeDecorator[datetime]):
    """Timezone-aware datetime that is stored as UTC and always returned aware."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:  # noqa: ARG002
        """Require aware values and normalize them to UTC."""
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime is not allowed; use timezone-aware UTC")
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:  # noqa: ARG002
        """Attach UTC to values from backends that drop the timezone (SQLite)."""
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def _enum[E: StrEnum](cls: type[E]) -> Enum:
    """Store a StrEnum as a plain string column (portable, no native enum type)."""
    return Enum(
        cls,
        native_enum=False,
        length=ENUM_LENGTH,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
    )


class Base(DeclarativeBase):
    """Declarative base with a stable constraint naming convention."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class User(Base):
    """A platform user."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class Session(Base):
    """A server-side login session; only the token hash is stored."""

    __tablename__ = "sessions"
    __table_args__ = (
        Index("ix_sessions_user_id", "user_id"),
        Index("ix_sessions_expires_at", "expires_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime)


class ApiToken(Base):
    """A scoped bearer token for agents; only the SHA-256 hash of the token is stored."""

    __tablename__ = "api_tokens"
    __table_args__ = (Index("ix_api_tokens_created_by", "created_by"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    token_prefix: Mapped[str] = mapped_column(String(16))
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    scopes: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)


class Project(Base):
    """A deployable project backed by one GitHub repository."""

    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    repository: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    archived_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)


class Environment(Base):
    """Development or production environment of a project."""

    __tablename__ = "environments"
    __table_args__ = (UniqueConstraint("project_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    name: Mapped[EnvironmentName] = mapped_column(_enum(EnvironmentName))
    branch: Mapped[str] = mapped_column(String(200))
    auto_deploy: Mapped[bool] = mapped_column(default=False)
    compose_project: Mapped[str | None] = mapped_column(String(63), default=None)
    compose_files: Mapped[list[str]] = mapped_column(JSON, default=list)
    services: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class PipelineDefinition(Base):
    """One immutable version of an environment's stored pipeline (append-only)."""

    __tablename__ = "pipeline_definitions"
    __table_args__ = (UniqueConstraint("environment_id", "version"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    environment_id: Mapped[int] = mapped_column(ForeignKey("environments.id", ondelete="CASCADE"))
    version: Mapped[int]
    jenkinsfile: Mapped[str] = mapped_column(Text)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class Run(Base):
    """One execution of a pipeline for an environment."""

    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("environment_id", "number"),
        Index("ix_runs_status", "status"),
        Index("ix_runs_environment_id_created_at", "environment_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    environment_id: Mapped[int] = mapped_column(ForeignKey("environments.id", ondelete="RESTRICT"))
    number: Mapped[int]
    trigger: Mapped[RunTrigger] = mapped_column(_enum(RunTrigger))
    branch: Mapped[str] = mapped_column(String(200))
    commit_sha: Mapped[str] = mapped_column(String(40))
    pipeline_source: Mapped[PipelineSource] = mapped_column(_enum(PipelineSource))
    pipeline_text: Mapped[str] = mapped_column(Text)
    status: Mapped[RunStatus] = mapped_column(_enum(RunStatus), default=RunStatus.QUEUED)
    triggered_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    triggered_by_token_id: Mapped[int | None] = mapped_column(
        ForeignKey("api_tokens.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    message: Mapped[str | None] = mapped_column(String(500), default=None)


class StageRun(Base):
    """Execution record of one pipeline stage."""

    __tablename__ = "stage_runs"
    __table_args__ = (UniqueConstraint("run_id", "position"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    position: Mapped[int]
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[RunStatus] = mapped_column(_enum(RunStatus), default=RunStatus.QUEUED)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)


class StepRun(Base):
    """Execution record of one step inside a stage."""

    __tablename__ = "step_runs"
    __table_args__ = (UniqueConstraint("stage_run_id", "position"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    stage_run_id: Mapped[int] = mapped_column(ForeignKey("stage_runs.id", ondelete="CASCADE"))
    position: Mapped[int]
    step_type: Mapped[str] = mapped_column(String(32))
    label: Mapped[str] = mapped_column(String(500))
    status: Mapped[RunStatus] = mapped_column(_enum(RunStatus), default=RunStatus.QUEUED)
    exit_code: Mapped[int | None] = mapped_column(default=None)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)


class Secret(Base):
    """An encrypted per-environment secret; the plaintext is never stored."""

    __tablename__ = "secrets"
    __table_args__ = (UniqueConstraint("environment_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    environment_id: Mapped[int] = mapped_column(ForeignKey("environments.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(128))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class AuditEvent(Base):
    """Append-only record of a security-relevant action; details never hold secrets."""

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_occurred_at", "occurred_at"),
        Index("ix_audit_events_target_type_target_id", "target_type", "target_id"),
        Index("ix_audit_events_action", "action"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    actor_type: Mapped[ActorType] = mapped_column(_enum(ActorType))
    actor_id: Mapped[str | None] = mapped_column(String(64), default=None)
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[str | None] = mapped_column(String(64), default=None)
    details: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
