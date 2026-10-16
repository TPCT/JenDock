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
    BigInteger,
    DateTime,
    Dialect,
    Enum,
    Float,
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

from backend.domain.access import AccessRole, GlobalRole
from backend.domain.clock import utcnow
from backend.domain.database import DatabaseEngine, TlsMode
from backend.domain.discovery import Confidence
from backend.domain.enums import (
    ActorType,
    DeploymentStatus,
    EnvironmentName,
    GitTransport,
    PipelineSource,
    RunStatus,
    RunTrigger,
)
from backend.domain.ingress import DnsRecordType, DomainOrigin, DomainStatus

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
    role: Mapped[GlobalRole] = mapped_column(
        _enum(GlobalRole), default=GlobalRole.MEMBER, server_default=GlobalRole.MEMBER.value
    )
    display_name: Mapped[str] = mapped_column(String(120), default="", server_default="")
    email: Mapped[str | None] = mapped_column(String(254), default=None)
    must_change_password: Mapped[bool] = mapped_column(default=False, server_default="0")

    # Two-factor authentication: the TOTP key encrypted by the worker (never plaintext at
    # rest). It is pending until ``totp_enabled_at`` is set by a successful verification.
    totp_secret: Mapped[bytes | None] = mapped_column(LargeBinary, default=None)
    totp_enabled_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    totp_last_step: Mapped[int | None] = mapped_column(default=None)


class RecoveryCode(Base):
    """A one-time two-factor recovery code; only its SHA-256 hash is stored."""

    __tablename__ = "recovery_codes"
    __table_args__ = (Index("ix_recovery_codes_user_id", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    code_hash: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    used_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)


class AccessGrant(Base):
    """What a member may do on a project, one of its environments or one container service.

    ``scope_key`` (``project``, ``env:<name>`` or ``svc:<name>:<service>``) makes a grant unique
    per user and target even though ``environment`` and ``service`` are nullable. Project and
    environment grants carry ``role``; service grants carry ``permissions``
    (``logs``, ``terminal``, ``actions``). Grants are removed with their user and project.
    """

    __tablename__ = "access_grants"
    __table_args__ = (
        UniqueConstraint("user_id", "project_id", "scope_key"),
        Index("ix_access_grants_project_id", "project_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    environment: Mapped[EnvironmentName | None] = mapped_column(
        _enum(EnvironmentName), default=None
    )
    service: Mapped[str | None] = mapped_column(String(64), default=None)
    scope_key: Mapped[str] = mapped_column(String(100))
    role: Mapped[AccessRole | None] = mapped_column(_enum(AccessRole), default=None)
    permissions: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
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
    # Client address (from the trusted proxy) and a short user agent, for the sessions list.
    ip_address: Mapped[str | None] = mapped_column(String(64), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(255), default=None)


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
    git_transport: Mapped[GitTransport] = mapped_column(
        _enum(GitTransport), default=GitTransport.HTTPS, server_default=GitTransport.HTTPS.value
    )
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
    include_in_backups: Mapped[bool] = mapped_column(default=False)
    compose_project: Mapped[str | None] = mapped_column(String(63), default=None)
    compose_files: Mapped[list[str]] = mapped_column(JSON, default=list)
    # Interpolation env files for every compose invocation (absolute host paths).
    compose_env_files: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")
    services: Mapped[list[str]] = mapped_column(JSON, default=list)
    auto_stop_hours: Mapped[int | None] = mapped_column(default=None)
    started_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
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


class Deployment(Base):
    """One rollout of built images to the services of an environment.

    ``services`` holds one object per service: ``service``, ``previous_image``,
    ``previous_image_id``, ``new_image`` and ``new_image_id`` (the previous ones are null when
    the service was not running). A redeploy starts as a ``pending`` row that points at the
    deployment whose images it reuses.
    """

    __tablename__ = "deployments"
    __table_args__ = (
        Index("ix_deployments_environment_id_created_at", "environment_id", "created_at"),
        Index("ix_deployments_run_id", "run_id"),
        Index("ix_deployments_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"))
    environment_id: Mapped[int] = mapped_column(ForeignKey("environments.id", ondelete="RESTRICT"))
    status: Mapped[DeploymentStatus] = mapped_column(
        _enum(DeploymentStatus), default=DeploymentStatus.DEPLOYING
    )
    commit_sha: Mapped[str] = mapped_column(String(40))
    services: Mapped[list[dict[str, str | None]]] = mapped_column(JSON, default=list)
    message: Mapped[str | None] = mapped_column(String(500), default=None)
    redeploy_of_id: Mapped[int | None] = mapped_column(
        ForeignKey("deployments.id", ondelete="SET NULL"), default=None
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
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


class DatabaseConnection(Base):
    """A database an environment's inspector may connect to.

    ``password_secret`` names a secret of the same environment (the password itself is only
    ever stored encrypted there). Rows go away with their environment.
    """

    __tablename__ = "database_connections"
    __table_args__ = (UniqueConstraint("environment_id", "name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    environment_id: Mapped[int] = mapped_column(ForeignKey("environments.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(48))
    engine: Mapped[DatabaseEngine] = mapped_column(_enum(DatabaseEngine))
    host: Mapped[str] = mapped_column(String(253))
    port: Mapped[int]
    database: Mapped[str] = mapped_column(String(128))
    username: Mapped[str] = mapped_column(String(128))
    password_secret: Mapped[str] = mapped_column(String(128))
    tls_mode: Mapped[TlsMode] = mapped_column(_enum(TlsMode), default=TlsMode.DISABLE)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    last_tested_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    last_test_ok: Mapped[bool | None] = mapped_column(default=None)


class DatabaseBackup(Base):
    """One scheduled or manually requested database backup."""

    __tablename__ = "database_backups"
    __table_args__ = (
        Index("ix_database_backups_connection_id_started_at", "connection_id", "started_at"),
        Index("ix_database_backups_status", "status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    connection_id: Mapped[int] = mapped_column(
        ForeignKey("database_connections.id", ondelete="RESTRICT")
    )
    environment_id: Mapped[int] = mapped_column(ForeignKey("environments.id", ondelete="RESTRICT"))
    trigger: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, default=None)
    object_key: Mapped[str | None] = mapped_column(String(1024), default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)


class Domain(Base):
    """A public hostname of an environment, routed to one service port by Apache.

    ``hostname`` is unique across all projects. Environments are never deleted (projects are
    archived), so the reference is RESTRICT: removing one must first tear its ingress down.
    """

    __tablename__ = "domains"
    __table_args__ = (Index("ix_domains_environment_id", "environment_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    environment_id: Mapped[int] = mapped_column(ForeignKey("environments.id", ondelete="RESTRICT"))
    hostname: Mapped[str] = mapped_column(String(253), unique=True)
    service: Mapped[str] = mapped_column(String(63))
    container_port: Mapped[int]
    published_port: Mapped[int]
    dns_record_type: Mapped[DnsRecordType] = mapped_column(
        _enum(DnsRecordType), default=DnsRecordType.A
    )
    dns_target: Mapped[str | None] = mapped_column(String(255), default=None)
    dns_proxied: Mapped[bool] = mapped_column(default=False)
    tls_enabled: Mapped[bool] = mapped_column(default=True)
    status: Mapped[DomainStatus] = mapped_column(_enum(DomainStatus), default=DomainStatus.DRAFT)
    status_message: Mapped[str | None] = mapped_column(String(500), default=None)
    cloudflare_zone_id: Mapped[str | None] = mapped_column(String(64), default=None)
    cloudflare_record_id: Mapped[str | None] = mapped_column(String(64), default=None)
    applied_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    last_verified_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    last_verified_ok: Mapped[bool | None] = mapped_column(default=None)
    # ``adopted`` domains existed before Jendock: only recorded here, DNS and Apache stay as
    # they are. ``vhost_file`` is the Apache file the adoption saw (information only).
    origin: Mapped[DomainOrigin] = mapped_column(
        _enum(DomainOrigin), default=DomainOrigin.MANAGED, server_default=DomainOrigin.MANAGED.value
    )
    adopted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    vhost_file: Mapped[str | None] = mapped_column(String(255), default=None)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class DiscoveredDomain(Base):
    """A host name found on Cloudflare and/or in Apache by the last discovery (read-only data).

    The worker replaces all rows on every refresh. Whether a row is linked to a project is not
    stored: it is read from ``domains`` by host name, so adopting never has to edit this table.
    """

    __tablename__ = "discovered_domains"

    id: Mapped[int] = mapped_column(primary_key=True)
    hostname: Mapped[str] = mapped_column(String(253), unique=True)
    zone_name: Mapped[str | None] = mapped_column(String(253), default=None)
    zone_id: Mapped[str | None] = mapped_column(String(64), default=None)
    record_id: Mapped[str | None] = mapped_column(String(64), default=None)
    record_type: Mapped[str | None] = mapped_column(String(8), default=None)
    record_content: Mapped[str | None] = mapped_column(String(255), default=None)
    proxied: Mapped[bool] = mapped_column(default=False)
    # ``None``: the server's address is not known, so nothing can be said.
    points_to_server: Mapped[bool | None] = mapped_column(default=None)
    vhost_file: Mapped[str | None] = mapped_column(String(255), default=None)
    vhost_managed: Mapped[bool] = mapped_column(default=False)
    upstream_host: Mapped[str | None] = mapped_column(String(255), default=None)
    upstream_port: Mapped[int | None] = mapped_column(default=None)
    vhost_ssl: Mapped[bool | None] = mapped_column(default=None)
    suggested_project: Mapped[str | None] = mapped_column(String(64), default=None)
    suggested_environment: Mapped[EnvironmentName | None] = mapped_column(
        _enum(EnvironmentName), default=None
    )
    suggested_service: Mapped[str | None] = mapped_column(String(63), default=None)
    suggested_container_port: Mapped[int | None] = mapped_column(default=None)
    suggested_published_port: Mapped[int | None] = mapped_column(default=None)
    suggestion_confidence: Mapped[Confidence | None] = mapped_column(
        _enum(Confidence), default=None
    )
    suggestion_reason: Mapped[str | None] = mapped_column(String(300), default=None)
    seen_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class DiscoveryState(Base):
    """What the last discovery could and could not read (one row, id 1)."""

    __tablename__ = "discovery_state"

    id: Mapped[int] = mapped_column(primary_key=True)
    refreshed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    cloudflare: Mapped[str] = mapped_column(String(16), default="unknown")
    cloudflare_message: Mapped[str | None] = mapped_column(String(300), default=None)
    helper: Mapped[str] = mapped_column(String(16), default="unknown")
    helper_message: Mapped[str | None] = mapped_column(String(300), default=None)
    server_ip: Mapped[str | None] = mapped_column(String(64), default=None)
    ip_source: Mapped[str] = mapped_column(String(16), default="unknown")
    zones: Mapped[list[str]] = mapped_column(JSON, default=list)
    record_count: Mapped[int] = mapped_column(default=0)


class ResourceLimit(Base):
    """CPU and memory limits of one compose service of an environment (at most one row).

    The row is the single source of truth: the managed compose override is rendered from it at
    every deploy and recreate, and "apply now" copies it onto the running containers. A column
    that is ``NULL`` means that limit is not set. Removed with its environment; ``updated_by``
    is ``NULL`` once that user is gone.
    """

    __tablename__ = "resource_limits"
    __table_args__ = (UniqueConstraint("environment_id", "service"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    environment_id: Mapped[int] = mapped_column(ForeignKey("environments.id", ondelete="CASCADE"))
    service: Mapped[str] = mapped_column(String(64))
    cpus: Mapped[float | None] = mapped_column(Float, default=None)
    memory_bytes: Mapped[int | None] = mapped_column(BigInteger, default=None)
    memory_reservation_bytes: Mapped[int | None] = mapped_column(BigInteger, default=None)
    cpu_shares: Mapped[int | None] = mapped_column(default=None)
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), default=None
    )
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    origin: Mapped[str] = mapped_column(String(16), default="user", server_default="user")


class DomainCertificate(Base):
    """The current certificate of a domain. Key material is never stored here."""

    __tablename__ = "domain_certificates"

    id: Mapped[int] = mapped_column(primary_key=True)
    domain_id: Mapped[int] = mapped_column(
        ForeignKey("domains.id", ondelete="CASCADE"), unique=True
    )
    serial: Mapped[str] = mapped_column(String(64))
    fingerprint_sha256: Mapped[str] = mapped_column(String(64))
    challenge: Mapped[str] = mapped_column(String(16))
    not_before: Mapped[datetime] = mapped_column(UtcDateTime)
    not_after: Mapped[datetime] = mapped_column(UtcDateTime)
    issued_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    last_attempt_at: Mapped[datetime | None] = mapped_column(UtcDateTime, default=None)
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(String(500), default=None)


class DomainCheck(Base):
    """The latest status check of a domain (DNS, HTTPS, backend); one row per domain."""

    __tablename__ = "domain_checks"

    id: Mapped[int] = mapped_column(primary_key=True)
    domain_id: Mapped[int] = mapped_column(
        ForeignKey("domains.id", ondelete="CASCADE"), unique=True
    )
    checked_at: Mapped[datetime] = mapped_column(UtcDateTime)
    ok: Mapped[bool]
    results: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)


class WebhookDelivery(Base):
    """A GitHub webhook delivery that was verified and handled (idempotency by delivery id)."""

    __tablename__ = "webhook_deliveries"
    __table_args__ = (Index("ix_webhook_deliveries_received_at", "received_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    delivery_id: Mapped[str] = mapped_column(String(64), unique=True)
    event: Mapped[str] = mapped_column(String(32))
    repository: Mapped[str | None] = mapped_column(String(160), default=None)
    outcome: Mapped[str] = mapped_column(String(32))
    branch: Mapped[str | None] = mapped_column(String(200), default=None)
    received_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class PlatformSecret(Base):
    """An encrypted platform-level secret (for example the Cloudflare API token).

    Only the worker holds the key that decrypts it; the API reads names and timestamps only.
    """

    __tablename__ = "platform_secrets"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


class PlatformSetting(Base):
    """A non-secret platform setting; the environment variable of the same name is the default."""

    __tablename__ = "platform_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime, default=utcnow)


import backend.repositories.notification_models  # noqa: E402, F401 - registers the tables
