"""Builds the worker's notification parts: emitter, notifiers, monitors, dispatcher, handlers.

One ``NotificationParts`` is created per worker process and handed to the pieces that emit
events (execution, deployment ledger, certificate renewal, webhook verification). Sending only
ever happens in the dispatcher thread, so emission points never wait for the network.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from backend.adapters.notify_senders import Deliverer, HttpPoster, Mailer, SmtpMailer, UrllibPoster
from backend.config import Settings
from backend.config_notifications import NotificationSettings, get_notification_settings
from backend.execution.logs import LogStore
from backend.logging import SecretRedactor
from backend.repositories.database import SessionFactory, UnitOfWork
from backend.services.notification_channels import ChannelService
from backend.services.notification_dispatcher import DispatchPolicy, NotificationDispatcher
from backend.services.notification_events import (
    CertificateNotifier,
    DeploymentNotifier,
    NoRunNotifications,
    RunNotifications,
    RunNotifier,
)
from backend.services.notification_monitors import DiskMonitor, WebhookFailureTracker
from backend.services.notification_outbox import EventSink, NoEvents, OutboxEmitter
from backend.services.secret_cipher import SecretCipher
from backend.worker.notification_handlers import build_notification_handlers
from backend.worker.rpc import Handler

LOGS_DIR_NAME = "logs"


class StopSignal(Protocol):
    """``ShutdownHandler`` as the loops see it."""

    def wait(self, timeout: float) -> bool:
        """Sleep up to ``timeout``; return True when shutdown was requested."""


@dataclass(frozen=True, slots=True)
class NotificationParts:
    """Everything notification related that the worker wires into other components."""

    sink: EventSink
    runs: RunNotifications
    deployments: DeploymentNotifier
    certificates: CertificateNotifier
    webhook: WebhookFailureTracker
    handlers: Mapping[str, Handler]
    dispatcher: NotificationDispatcher | None
    disk: DiskMonitor | None

    def start(self, shutdown: StopSignal) -> list[threading.Thread]:
        """Start the dispatcher and the disk monitor on daemon threads (none when disabled)."""
        threads: list[threading.Thread] = []
        if self.dispatcher is not None:
            threads.append(_thread("notification-dispatch", self.dispatcher.run_forever, shutdown))
        if self.disk is not None:
            threads.append(_thread("notification-disk", self.disk.run_forever, shutdown))
        return threads


def _thread(name: str, target: object, shutdown: StopSignal) -> threading.Thread:
    thread = threading.Thread(target=target, args=(shutdown,), name=name, daemon=True)  # type: ignore[arg-type]
    thread.start()
    return thread


def build_notification_parts(  # noqa: PLR0913, PLR0917 - one optional override each
    settings: Settings,
    session_factory: SessionFactory,
    cipher: SecretCipher,
    redactor: SecretRedactor,
    notify: NotificationSettings | None = None,
    poster: HttpPoster | None = None,
    mailer: Mailer | None = None,
) -> NotificationParts:
    """Wire the notification subsystem from settings; tests inject fake senders."""
    config = notify or get_notification_settings()

    def uow() -> UnitOfWork:
        return UnitOfWork(session_factory)

    deliverer = Deliverer(
        poster or UrllibPoster(),
        mailer or SmtpMailer(),
        settings.public_origin,
        config.send_timeout_seconds,
    )
    sink: EventSink = OutboxEmitter(uow, config.dedupe_minutes) if config.enabled else NoEvents()
    logs = LogStore(settings.data_dir / LOGS_DIR_NAME)
    runs: RunNotifications = (
        RunNotifier(uow, logs, sink, redactor) if config.enabled else NoRunNotifications()
    )
    policy = DispatchPolicy(
        max_attempts=config.max_attempts,
        retry_base_seconds=config.retry_base_seconds,
        history_days=config.history_days,
        poll_seconds=config.poll_interval_seconds,
    )
    return NotificationParts(
        sink=sink,
        runs=runs,
        deployments=DeploymentNotifier(uow, sink, redactor),
        certificates=CertificateNotifier(uow, sink, redactor),
        webhook=WebhookFailureTracker(
            sink,
            config.webhook_failure_threshold,
            timedelta(minutes=config.webhook_failure_window_minutes),
        ),
        handlers=build_notification_handlers(lambda: ChannelService(uow(), cipher, deliverer)),
        dispatcher=(
            NotificationDispatcher(uow, cipher, deliverer, policy) if config.enabled else None
        ),
        disk=(
            DiskMonitor(
                settings.data_dir,
                config.disk_threshold_percent,
                sink,
                config.disk_check_seconds,
            )
            if config.enabled
            else None
        ),
    )
