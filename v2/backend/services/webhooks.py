"""Handling verified GitHub webhook deliveries (idempotent, audited)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum

from sqlalchemy.exc import IntegrityError

from backend.domain.actor import Actor
from backend.domain.audit import AuditEntry, AuditTarget
from backend.domain.clock import Clock, utcnow
from backend.domain.enums import RunTrigger
from backend.domain.errors import ConflictError
from backend.domain.github_events import PushEvent, parse_push, repository_of
from backend.repositories.database import UnitOfWork
from backend.repositories.models import Project, WebhookDelivery
from backend.services.audit import add_audit_event
from backend.services.runs import RunRequest, queue_run

DELIVERY_RETENTION = timedelta(days=30)
GITHUB_ACTOR = "github"
RACE_ATTEMPTS = 3


class WebhookOutcome(StrEnum):
    """What a delivery led to."""

    PONG = "pong"  # a ``ping`` event
    QUEUED = "queued"  # at least one run was queued
    RECORDED = "recorded"  # a tracked branch moved but auto-deploy is off: no run
    IGNORED = "ignored"  # nothing to do (other event, tag, branch delete, unknown repository...)
    DUPLICATE = "duplicate"  # this delivery id was handled before


@dataclass(frozen=True, slots=True)
class WebhookResult:
    """Outcome of one delivery, with the ids of the runs it queued."""

    outcome: WebhookOutcome
    run_ids: tuple[int, ...] = ()
    reason: str | None = None


@dataclass(slots=True)
class _Routing:
    """Mutable result of routing one event inside the transaction."""

    outcome: WebhookOutcome
    reason: str | None = None
    repository: str | None = None
    branch: str | None = None
    run_ids: list[int] = field(default_factory=list)


class WebhookService:
    """Routes ``ping`` and ``push`` deliveries; a replayed delivery id changes nothing.

    The delivery row, the runs and the audit events are committed in one transaction, so a
    delivery is either fully handled (and remembered) or not at all (and GitHub may retry).
    """

    def __init__(self, uow: UnitOfWork, clock: Clock = utcnow) -> None:
        self._uow = uow
        self._clock = clock

    def handle(self, delivery_id: str, event: str, payload: Mapping[str, object]) -> WebhookResult:
        """Process one delivery whose signature was already verified."""
        for attempt in range(RACE_ATTEMPTS):
            try:
                return self._handle_once(delivery_id, event, payload)
            except IntegrityError as error:
                if self._already_handled(delivery_id):
                    return WebhookResult(WebhookOutcome.DUPLICATE)
                if attempt == RACE_ATTEMPTS - 1:
                    raise ConflictError("run number was taken concurrently; retry") from error
        raise ConflictError("webhook could not be processed")  # pragma: no cover

    def _already_handled(self, delivery_id: str) -> bool:
        with self._uow:
            return self._uow.deliveries.exists(delivery_id)

    def _handle_once(
        self, delivery_id: str, event: str, payload: Mapping[str, object]
    ) -> WebhookResult:
        with self._uow:
            if self._uow.deliveries.exists(delivery_id):
                return WebhookResult(WebhookOutcome.DUPLICATE)
            now = self._clock()
            self._uow.deliveries.delete_older_than(now - DELIVERY_RETENTION)
            routing = self._route(event, payload)
            delivery = WebhookDelivery(
                delivery_id=delivery_id,
                event=event[:32],
                repository=routing.repository,
                outcome=routing.outcome.value,
                received_at=now,
            )
            self._uow.deliveries.add(delivery)
            self._audit(delivery_id, event, routing)
            return WebhookResult(routing.outcome, tuple(routing.run_ids), routing.reason)

    def _route(self, event: str, payload: Mapping[str, object]) -> _Routing:
        if event == "ping":
            return _Routing(WebhookOutcome.PONG, repository=repository_of(payload))
        if event != "push":
            return _Routing(WebhookOutcome.IGNORED, f"event {event[:32]!r} is not handled")
        push = parse_push(payload)
        routing = _Routing(WebhookOutcome.IGNORED, repository=push.repository, branch=push.branch)
        if push.branch is None:
            routing.reason = "not a branch push"
        elif push.is_branch_delete:
            routing.reason = "branch deleted"
        else:
            self._route_push(push, push.branch, routing)
        return routing

    def _route_push(self, push: PushEvent, branch: str, routing: _Routing) -> None:
        projects = self._uow.projects.list_by_repository(push.repository)
        if not projects:
            routing.reason = "repository is not allowlisted"
            return
        tracked = 0
        for project in projects:
            tracked += self._route_project(project, push, branch, routing)
        if routing.run_ids:
            routing.outcome = WebhookOutcome.QUEUED
        elif tracked:
            routing.outcome = WebhookOutcome.RECORDED
            routing.reason = "auto-deploy is off"
        else:
            routing.reason = "no environment tracks this branch"

    def _route_project(
        self, project: Project, push: PushEvent, branch: str, routing: _Routing
    ) -> int:
        """Queue runs for the environments of ``project`` that track ``branch``; return how
        many environments track it."""
        tracked = 0
        for environment in self._uow.environments.list_for_project(project.id):
            if environment.branch != branch:
                continue
            tracked += 1
            if not environment.auto_deploy:
                continue
            request = RunRequest(
                RunTrigger.PUSH,
                branch,
                push.after,
                audit_actor=Actor.system(GITHUB_ACTOR),
                audit_details={"project": project.slug},
            )
            routing.run_ids.append(queue_run(self._uow, self._clock, environment, request).id)
        return tracked

    def _audit(self, delivery_id: str, event: str, routing: _Routing) -> None:
        details: dict[str, object] = {
            "delivery": delivery_id,
            "event": event[:32],
            "repository": routing.repository,
            "branch": routing.branch,
            "outcome": routing.outcome.value,
            "reason": routing.reason,
            "runs": list(routing.run_ids),
        }
        target = AuditTarget("webhook", delivery_id)
        entry = AuditEntry(Actor.system(GITHUB_ACTOR), "github.webhook", target, details)
        add_audit_event(self._uow, entry, self._clock)
