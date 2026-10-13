"""The home dashboard's database read model: a fixed number of queries, bounded sizes.

Each card is one query (joins instead of per-row lookups), so the cost does not grow with the
number of projects. Host telemetry is not here: it comes from the worker and is added by the
API layer.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.domain.clock import Clock, utcnow
from backend.domain.enums import DeploymentStatus, EnvironmentName, RunStatus, RunTrigger
from backend.repositories.database import UnitOfWork
from backend.repositories.models import (
    ApiToken,
    Deployment,
    Domain,
    DomainCertificate,
    Environment,
    Project,
    Run,
    User,
    WebhookDelivery,
)

CARD_LIMIT = 10
# Rows read per card before the caller's per-environment filter; keeps a member's cards full
# without a query per row.
FETCH_LIMIT = 50
EnvironmentCheck = Callable[[str, EnvironmentName], bool]
PUSH_WINDOW = timedelta(days=7)
CERTIFICATE_WINDOW = timedelta(days=30)
ATTENTION_STATUSES = (RunStatus.FAILED, RunStatus.INTERRUPTED)
ACTIVE_STATUSES = (RunStatus.QUEUED, RunStatus.RUNNING)


@dataclass(frozen=True, slots=True)
class DashboardScope:
    """What the caller may see; ``None`` means everything (owners and admins).

    ``projects`` narrows every query to the projects the caller holds a grant on; the
    per-environment checks (built from the policy by ``authz.dashboard_scope``) then decide card
    by card. ``host`` says whether the host card may be shown at all.
    """

    projects: frozenset[str] | None = None
    sees: EnvironmentCheck | None = None
    deployments: EnvironmentCheck | None = None
    runs: EnvironmentCheck | None = None
    domains: EnvironmentCheck | None = None
    host: bool = True


ALL = DashboardScope()


@dataclass(frozen=True, slots=True)
class DeploymentItem:
    """A recent deployment with who triggered it."""

    id: int
    project: str
    project_name: str
    environment: EnvironmentName
    status: DeploymentStatus
    commit_sha: str
    run_id: int
    run_number: int
    trigger: RunTrigger
    triggered_by: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class AttentionItem:
    """A run that is active or whose environment currently ends in a failure."""

    run_id: int
    project: str
    project_name: str
    environment: EnvironmentName
    number: int
    status: RunStatus
    branch: str
    commit_sha: str
    trigger: RunTrigger
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    message: str | None


@dataclass(frozen=True, slots=True)
class ManualDeployItem:
    """An environment with auto-deploy off that received pushes recently."""

    project: str
    project_name: str
    environment: EnvironmentName
    branch: str
    pushes: int
    last_push_at: datetime


@dataclass(frozen=True, slots=True)
class CertificateItem:
    """A certificate that expires soon (or already has)."""

    hostname: str
    project: str
    environment: EnvironmentName
    not_after: datetime
    expired: bool


@dataclass(frozen=True, slots=True)
class DashboardData:
    """Everything the database contributes to the dashboard."""

    project_count: int
    deployments: tuple[DeploymentItem, ...]
    attention: tuple[AttentionItem, ...]
    manual_deploys: tuple[ManualDeployItem, ...]
    certificates: tuple[CertificateItem, ...]
    domain_count: int


class DashboardService:
    """Loads the dashboard cards."""

    def __init__(self, uow: UnitOfWork, clock: Clock = utcnow) -> None:
        self._uow = uow
        self._clock = clock

    def load(self, scope: DashboardScope = ALL) -> DashboardData:
        """All cards in one read transaction, limited to what ``scope`` allows."""
        now = self._clock()
        with self._uow:
            session = self._uow.session
            return DashboardData(
                project_count=_count_projects(session, scope),
                deployments=_recent_deployments(session, scope),
                attention=_attention(session, scope),
                manual_deploys=_manual_deploys(session, scope, now - PUSH_WINDOW),
                certificates=_certificates(session, scope, now + CERTIFICATE_WINDOW, now),
                domain_count=_count_domains(session, scope),
            )


def _only[T](
    rows: list[T], keep: EnvironmentCheck | None, key: Callable[[T], tuple[str, EnvironmentName]]
) -> list[T]:
    """The first ``CARD_LIMIT`` rows the caller may see."""
    if keep is None:
        return rows[:CARD_LIMIT]
    return [row for row in rows if keep(*key(row))][:CARD_LIMIT]


def _fetch_limit(scope: DashboardScope) -> int:
    return CARD_LIMIT if scope.projects is None else FETCH_LIMIT


def _in_scope(query: Any, scope: DashboardScope) -> Any:
    if scope.projects is None:
        return query
    return query.where(Project.slug.in_(sorted(scope.projects)))


def _count_projects(session: Session, scope: DashboardScope) -> int:
    query = select(func.count()).select_from(Project).where(Project.archived_at.is_(None))
    return session.scalar(_in_scope(query, scope)) or 0


def _count_domains(session: Session, scope: DashboardScope) -> int:
    query = (
        select(Domain.id, Project.slug, Environment.name)
        .join(Environment, Environment.id == Domain.environment_id)
        .join(Project, Project.id == Environment.project_id)
        .where(Project.archived_at.is_(None))
    )
    rows = session.execute(_in_scope(query, scope)).all()
    keep = scope.domains
    return sum(1 for _id, slug, env in rows if keep is None or keep(slug, env))


def _recent_deployments(session: Session, scope: DashboardScope) -> tuple[DeploymentItem, ...]:
    query = (
        select(Deployment, Run, Project, Environment.name, User.username, ApiToken.name)
        .join(Run, Run.id == Deployment.run_id)
        .join(Environment, Environment.id == Deployment.environment_id)
        .join(Project, Project.id == Environment.project_id)
        .outerjoin(User, User.id == Run.triggered_by)
        .outerjoin(ApiToken, ApiToken.id == Run.triggered_by_token_id)
        .where(Project.archived_at.is_(None))
        .order_by(Deployment.id.desc())
        .limit(_fetch_limit(scope))
    )
    rows = _only(
        list(session.execute(_in_scope(query, scope)).all()),
        scope.deployments,
        lambda row: (row[2].slug, row[3]),
    )
    items = []
    for deployment, run, project, env_name, username, token_name in rows:
        who = username or (f"token {token_name}" if token_name else None)
        items.append(
            DeploymentItem(
                id=deployment.id,
                project=project.slug,
                project_name=project.name,
                environment=env_name,
                status=deployment.status,
                commit_sha=deployment.commit_sha,
                run_id=run.id,
                run_number=run.number,
                trigger=run.trigger,
                triggered_by=who,
                created_at=deployment.created_at,
                started_at=deployment.started_at,
                finished_at=deployment.finished_at,
            )
        )
    return tuple(items)


def _attention_item(run: Run, project: Project, env_name: EnvironmentName) -> AttentionItem:
    return AttentionItem(
        run_id=run.id,
        project=project.slug,
        project_name=project.name,
        environment=env_name,
        number=run.number,
        status=run.status,
        branch=run.branch,
        commit_sha=run.commit_sha,
        trigger=run.trigger,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        message=run.message,
    )


def _attention(session: Session, scope: DashboardScope) -> tuple[AttentionItem, ...]:
    """Running and queued runs, plus environments whose newest run failed."""
    base = (
        select(Run, Project, Environment.name)
        .join(Environment, Environment.id == Run.environment_id)
        .join(Project, Project.id == Environment.project_id)
        .where(Project.archived_at.is_(None))
    )
    base = _in_scope(base, scope)
    limit = _fetch_limit(scope)
    active = (
        base.where(Run.status.in_(ACTIVE_STATUSES))
        .order_by(Run.status.desc(), Run.id.desc())  # "running" sorts before "queued" descending
        .limit(limit)
    )
    newest = (
        select(func.max(Run.id).label("run_id")).group_by(Run.environment_id).subquery("newest")
    )
    failing = (
        base.join(newest, newest.c.run_id == Run.id)
        .where(Run.status.in_(ATTENTION_STATUSES))
        .order_by(Run.id.desc())
        .limit(limit)
    )
    key = lambda row: (row[1].slug, row[2])  # noqa: E731
    rows = [
        *_only(list(session.execute(active).all()), scope.runs, key),
        *_only(list(session.execute(failing).all()), scope.runs, key),
    ]
    return tuple(_attention_item(run, project, env) for run, project, env in rows)


def _manual_deploys(
    session: Session, scope: DashboardScope, cutoff: datetime
) -> tuple[ManualDeployItem, ...]:
    query = (
        select(
            Project.slug,
            Project.name,
            Environment.name,
            Environment.branch,
            func.count(WebhookDelivery.id),
            func.max(WebhookDelivery.received_at),
        )
        .join(Project, Project.repository == WebhookDelivery.repository)
        .join(
            Environment,
            (Environment.project_id == Project.id) & (Environment.branch == WebhookDelivery.branch),
        )
        .where(
            WebhookDelivery.outcome == "recorded",
            WebhookDelivery.received_at >= cutoff,
            Environment.auto_deploy.is_(False),
            Project.archived_at.is_(None),
        )
        .group_by(Project.slug, Project.name, Environment.name, Environment.branch)
        .order_by(func.max(WebhookDelivery.received_at).desc())
        .limit(_fetch_limit(scope))
    )
    rows = _only(
        list(session.execute(_in_scope(query, scope)).all()),
        scope.sees,
        lambda row: (row[0], row[2]),
    )
    return tuple(
        ManualDeployItem(slug, name, env, branch, pushes, last)
        for slug, name, env, branch, pushes, last in rows
    )


def _certificates(
    session: Session, scope: DashboardScope, horizon: datetime, now: datetime
) -> tuple[CertificateItem, ...]:
    query = (
        select(Domain.hostname, Project.slug, Environment.name, DomainCertificate.not_after)
        .join(Domain, Domain.id == DomainCertificate.domain_id)
        .join(Environment, Environment.id == Domain.environment_id)
        .join(Project, Project.id == Environment.project_id)
        .where(DomainCertificate.not_after <= horizon, Project.archived_at.is_(None))
        .order_by(DomainCertificate.not_after)
        .limit(_fetch_limit(scope))
    )
    rows = _only(
        list(session.execute(_in_scope(query, scope)).all()),
        scope.domains,
        lambda row: (row[1], row[2]),
    )
    return tuple(
        CertificateItem(hostname, slug, env, not_after, not_after <= now)
        for hostname, slug, env, not_after in rows
    )
