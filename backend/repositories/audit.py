"""Audit event queries."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import ColumnElement, String, and_, cast, or_, select
from sqlalchemy.orm import Session

from backend.domain.audit import AuditFilter
from backend.repositories.models import (
    AuditEvent,
    DatabaseConnection,
    Deployment,
    Domain,
    Environment,
    Project,
    Run,
    User,
)


def _project_condition(slug: str) -> ColumnElement[bool]:
    env_ids = (
        select(Environment.id)
        .join(Project, Project.id == Environment.project_id)
        .where(Project.slug == slug)
    )
    owned = (
        ("environment", select(cast(Environment.id, String)).where(Environment.id.in_(env_ids))),
        ("run", select(cast(Run.id, String)).where(Run.environment_id.in_(env_ids))),
        (
            "deployment",
            select(cast(Deployment.id, String)).where(Deployment.environment_id.in_(env_ids)),
        ),
        ("domain", select(cast(Domain.id, String)).where(Domain.environment_id.in_(env_ids))),
        (
            "database_connection",
            select(cast(DatabaseConnection.id, String)).where(
                DatabaseConnection.environment_id.in_(env_ids)
            ),
        ),
    )
    clauses = [and_(AuditEvent.target_type == "project", AuditEvent.target_id == slug)]
    clauses.extend(
        and_(AuditEvent.target_type == kind, AuditEvent.target_id.in_(ids)) for kind, ids in owned
    )
    return or_(*clauses)


def _conditions(criteria: AuditFilter) -> list[ColumnElement[bool]]:  # noqa: PLR0912 - one branch per filter
    found: list[ColumnElement[bool]] = []
    if criteria.actor_type:
        found.append(AuditEvent.actor_type == criteria.actor_type)
    if criteria.actor_id:
        found.append(AuditEvent.actor_id == criteria.actor_id)
    if criteria.action:
        if criteria.action.endswith("*"):
            found.append(AuditEvent.action.startswith(criteria.action[:-1], autoescape=True))
        else:
            found.append(AuditEvent.action == criteria.action)
    if criteria.target_type:
        found.append(AuditEvent.target_type == criteria.target_type)
    if criteria.target_id:
        found.append(AuditEvent.target_id == criteria.target_id)
    if criteria.since:
        found.append(AuditEvent.occurred_at >= criteria.since)
    if criteria.until:
        found.append(AuditEvent.occurred_at < criteria.until)
    if criteria.project:
        found.append(_project_condition(criteria.project))
    if criteria.involving_user is not None:
        uid = str(criteria.involving_user)
        mine = and_(AuditEvent.actor_type == "user", AuditEvent.actor_id == uid)
        about_me = and_(AuditEvent.target_type == "user", AuditEvent.target_id == uid)
        found.append(or_(mine, about_me))
    if criteria.restrict_user is not None:
        found.append(_member_condition(criteria))
    if criteria.text:
        found.append(_text_condition(criteria.text))
    return found


# Targets that are not part of any project: a member's own account events stay visible.
_PROJECTLESS_TARGETS = ("user", "api_token")


def _member_condition(criteria: AuditFilter) -> ColumnElement[bool]:
    """A member's events: their own activity, minus anything about a project they cannot see."""
    uid = str(criteria.restrict_user)
    mine = or_(
        and_(AuditEvent.actor_type == "user", AuditEvent.actor_id == uid),
        and_(AuditEvent.target_type == "user", AuditEvent.target_id == uid),
    )
    allowed: list[ColumnElement[bool]] = [AuditEvent.target_type.in_(_PROJECTLESS_TARGETS)]
    allowed.extend(_project_condition(slug) for slug in criteria.visible_projects)
    return and_(mine, or_(*allowed))


def _text_condition(text: str) -> ColumnElement[bool]:
    named = select(cast(User.id, String)).where(User.username.icontains(text, autoescape=True))
    return or_(
        AuditEvent.action.icontains(text, autoescape=True),
        AuditEvent.target_id.icontains(text, autoescape=True),
        cast(AuditEvent.details, String).icontains(text, autoescape=True),
        and_(AuditEvent.actor_type == "user", AuditEvent.actor_id.in_(named)),
    )


class AuditRepository:
    """Queries for ``AuditEvent``. Events are append-only."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, event: AuditEvent) -> AuditEvent:
        """Persist ``event`` and assign its id."""
        self._session.add(event)
        self._session.flush()
        return event

    def search(
        self, criteria: AuditFilter, *, before_id: int | None, limit: int
    ) -> list[AuditEvent]:
        """Events matching ``criteria``, newest first (by id), strictly older than ``before_id``.

        Keyset pagination: pass the id of the last event of the previous page.
        """
        query = select(AuditEvent).where(*_conditions(criteria))
        if before_id is not None:
            query = query.where(AuditEvent.id < before_id)
        query = query.order_by(AuditEvent.id.desc()).limit(limit)
        return list(self._session.scalars(query).all())

    def list(
        self,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
        action: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[AuditEvent]:
        """Return events newest first, optionally filtered, with pagination."""
        query = select(AuditEvent).order_by(AuditEvent.occurred_at.desc(), AuditEvent.id.desc())
        if target_type is not None:
            query = query.where(AuditEvent.target_type == target_type)
        if target_id is not None:
            query = query.where(AuditEvent.target_id == target_id)
        if action is not None:
            query = query.where(AuditEvent.action == action)
        return self._session.scalars(query.limit(limit).offset(offset)).all()
