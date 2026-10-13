"""Notification rules and read models (API side; no secrets involved).

A scope is the global default, a project, or one environment of a project. A scope with any
rule row decides for its resources; otherwise the next wider scope does (environment, then
project, then default). An empty set of events (or a disabled rule) mutes a channel for that
scope without falling back to wider rules.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from backend.domain.actor import Actor
from backend.domain.audit import AuditEntry, AuditTarget
from backend.domain.clock import Clock, utcnow
from backend.domain.enums import EnvironmentName
from backend.domain.errors import NotFoundError, ValidationError
from backend.domain.notifications import parse_events
from backend.domain.paging import Page, PageQuery
from backend.repositories.database import UnitOfWork
from backend.repositories.notification_models import (
    NotificationChannel,
    NotificationDelivery,
    NotificationRule,
)
from backend.repositories.notifications import NotificationRepository
from backend.services.audit import add_audit_event
from backend.services.lookup import require_environment, require_project


@dataclass(frozen=True, slots=True)
class RuleScope:
    """Where a rule set applies: neither field set is the global default."""

    slug: str | None = None
    env: EnvironmentName | None = None

    @property
    def name(self) -> str:
        """``default``, ``project`` or ``environment``."""
        if self.slug is None:
            return "default"
        return "project" if self.env is None else "environment"


@dataclass(frozen=True, slots=True)
class RuleInput:
    """One channel's subscription as sent by a client."""

    channel_id: int
    events: list[str]
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class RuleView:
    """One stored rule."""

    channel_id: int
    events: list[str]
    enabled: bool


@dataclass(frozen=True, slots=True)
class RulesView:
    """The rules of a scope and, when the scope has none, which wider scope applies."""

    scope: str
    rules: list[RuleView]
    inherited_from: str | None


@dataclass(frozen=True, slots=True)
class DeliveryView:
    """A row of the delivery history."""

    row: NotificationDelivery
    channel_name: str
    project: str | None


def _repo(uow: UnitOfWork) -> NotificationRepository:
    return NotificationRepository(uow.session)


class NotificationRuleService:
    """Read and replace the rule set of a scope; audited."""

    def __init__(self, uow: UnitOfWork, clock: Clock = utcnow) -> None:
        self._uow = uow
        self._clock = clock

    def get(self, scope: RuleScope) -> RulesView:
        """The scope's own rules, plus which wider scope applies when it has none."""
        with self._uow:
            project_id, environment_id = self._ids(scope)
            repo = _repo(self._uow)
            own = repo.rules_in_scope(project_id, environment_id)
            inherited = None if own else self._inherited(repo, scope, project_id)
            return RulesView(scope.name, [_view(rule) for rule in own], inherited)

    def replace(self, scope: RuleScope, items: Sequence[RuleInput], actor: Actor) -> RulesView:
        """Replace the whole rule set of ``scope``; ``[]`` removes it (the wider scope applies)."""
        project_scoped = scope.slug is not None
        parsed = [
            (item, [e.value for e in parse_events(item.events, project_scoped=project_scoped)])
            for item in items
        ]
        ids = [item.channel_id for item, _ in parsed]
        if len(set(ids)) != len(ids):
            raise ValidationError("a channel can appear only once per rule set")
        now = self._clock()
        with self._uow:
            project_id, environment_id = self._ids(scope)
            repo = _repo(self._uow)
            for channel_id in ids:
                if repo.get_channel(channel_id) is None:
                    raise NotFoundError(f"notification channel not found: {channel_id}")
            repo.replace_rules(
                project_id,
                environment_id,
                [
                    NotificationRule(
                        project_id=project_id,
                        environment_id=environment_id,
                        channel_id=item.channel_id,
                        events=events,
                        enabled=item.enabled,
                        created_at=now,
                        updated_at=now,
                    )
                    for item, events in parsed
                ],
            )
            details: dict[str, object] = {
                "scope": scope.name,
                "rules": [
                    {"channel_id": i.channel_id, "events": events, "enabled": i.enabled}
                    for i, events in parsed
                ],
            }
            if scope.slug is not None:
                details["project"] = scope.slug
            if scope.env is not None:
                details["environment"] = scope.env.value
            target = AuditTarget("notification_rules", scope.slug or "default")
            add_audit_event(
                self._uow,
                AuditEntry(actor, "notification.rules.update", target, details),
                self._clock,
            )
        return self.get(scope)

    def _ids(self, scope: RuleScope) -> tuple[int | None, int | None]:
        if scope.slug is None:
            return None, None
        if scope.env is None:
            return require_project(self._uow, scope.slug).id, None
        environment = require_environment(self._uow, scope.slug, scope.env)
        return environment.project_id, environment.id

    @staticmethod
    def _inherited(
        repo: NotificationRepository, scope: RuleScope, project_id: int | None
    ) -> str | None:
        if scope.env is not None and repo.rules_in_scope(project_id, None):
            return "project"
        if scope.slug is not None and repo.rules_in_scope(None, None):
            return "default"
        return None


def _view(rule: NotificationRule) -> RuleView:
    return RuleView(rule.channel_id, list(rule.events), rule.enabled)


class NotificationQueryService:
    """Read models for the UI: channels and the delivery history."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    def channels(self) -> list[NotificationChannel]:
        """All channels by name (the encrypted configuration is never loaded)."""
        with self._uow:
            return list(_repo(self._uow).list_channels())

    def channel(self, channel_id: int) -> NotificationChannel:
        """One channel or ``NotFoundError``."""
        with self._uow:
            found = _repo(self._uow).get_channel(channel_id)
            if found is None:
                raise NotFoundError(f"notification channel not found: {channel_id}")
            return found

    def deliveries(
        self,
        query: PageQuery,
        *,
        slug: str | None,
        channel_id: int | None,
        status: str | None,
    ) -> Page[DeliveryView]:
        """The delivery history, newest first, optionally of one project/channel/status."""
        with self._uow:
            repo = _repo(self._uow)
            project_id = require_project(self._uow, slug).id if slug else None
            rows, total = repo.page(
                query, project_id=project_id, channel_id=channel_id, status=status
            )
            names = {c.id: c.name for c in repo.list_channels()}
            slugs = {p.id: p.slug for p in self._uow.projects.list(include_archived=True)}
            items = (
                DeliveryView(r, names.get(r.channel_id, "(deleted)"), slugs.get(r.project_id or 0))
                for r in rows
            )
            return Page.of(items, total, query)
