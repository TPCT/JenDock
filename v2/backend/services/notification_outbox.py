"""Turning an event into outbox rows: rule resolution and quiet-period deduplication.

``OutboxEmitter.emit`` is what every emission point calls. It only writes rows (the worker's
dispatcher sends them later), so an emission never waits for a network and never raises: a
failure here is logged and the run, deployment or renewal carries on.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Protocol

from backend.domain.clock import Clock, utcnow
from backend.domain.enums import EnvironmentName
from backend.domain.notifications import DeliveryStatus, EventPayload, event_info
from backend.repositories.database import UnitOfWork
from backend.repositories.notification_models import NotificationDelivery
from backend.repositories.notifications import NotificationRepository

logger = logging.getLogger(__name__)


class EventSink(Protocol):
    """Where emission points send events."""

    def emit(self, payload: EventPayload) -> int:
        """Queue ``payload`` for every subscribed channel; return how many rows were queued."""


class NoEvents:
    """Notifications are off: every event is dropped."""

    def emit(self, payload: EventPayload) -> int:  # noqa: ARG002
        """Queue nothing."""
        return 0


def quiet_period(payload: EventPayload, default_minutes: int) -> timedelta:
    """The collapse window of an event: its own minimum or the configured default."""
    return timedelta(minutes=max(default_minutes, event_info(payload.event).quiet_minutes))


class OutboxEmitter:
    """Resolves the rules of the event's scope and queues one row per subscribed channel."""

    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        dedupe_minutes: int,
        clock: Clock = utcnow,
    ) -> None:
        self._uow_factory = uow_factory
        self._dedupe_minutes = dedupe_minutes
        self._clock = clock

    def emit(self, payload: EventPayload) -> int:
        """Queue the event; never raises."""
        try:
            return self._emit(payload.bounded())
        except Exception:
            logger.exception("notification could not be queued", extra={"event": payload.event})
            return 0

    def _emit(self, payload: EventPayload) -> int:
        now = self._clock()
        since = now - quiet_period(payload, self._dedupe_minutes)
        queued = 0
        with self._uow_factory() as uow:
            repo = NotificationRepository(uow.session)
            project_id, environment_id = self._scope(uow, payload)
            rules = repo.effective_rules(project_id, environment_id)
            for rule in rules:
                if not rule.enabled or payload.event.value not in rule.events:
                    continue
                channel = repo.get_channel(rule.channel_id)
                if channel is None or not channel.enabled:
                    continue
                duplicate = repo.recent_duplicate(channel.id, payload.dedupe_key, since)
                if duplicate is not None:
                    duplicate.suppressed += 1
                    continue
                repo.add_delivery(
                    NotificationDelivery(
                        channel_id=channel.id,
                        project_id=project_id,
                        event=payload.event.value,
                        dedupe_key=payload.dedupe_key,
                        title=payload.title,
                        payload=payload.to_json(),
                        status=DeliveryStatus.PENDING.value,
                        next_attempt_at=now,
                        created_at=now,
                    )
                )
                queued += 1
        return queued

    @staticmethod
    def _scope(uow: UnitOfWork, payload: EventPayload) -> tuple[int | None, int | None]:
        """Ids of the project and environment the event belongs to (``None``: global event)."""
        if payload.project is None:
            return None, None
        project = uow.projects.get_by_slug(payload.project)
        if project is None:
            return None, None
        if payload.environment is None:
            return project.id, None
        environment = uow.environments.get_by_project_slug(
            payload.project, EnvironmentName(payload.environment)
        )
        return project.id, environment.id if environment else None
