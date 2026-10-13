"""Fakes and builders for notification tests: no network, no real SMTP."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path

from sqlalchemy import Engine

from backend.adapters.notify_senders import Deliverer, DeliveryError
from backend.domain.actor import Actor
from backend.domain.enums import EnvironmentName
from backend.domain.notifications import ChannelKind, EventPayload, NotificationEvent
from backend.repositories.database import UnitOfWork, create_session_factory
from backend.repositories.notification_models import NotificationDelivery
from backend.repositories.notifications import NotificationRepository
from backend.services.notification_channels import ChannelService
from backend.services.notification_dispatcher import DispatchPolicy, NotificationDispatcher
from backend.services.notification_outbox import OutboxEmitter
from backend.services.notification_rules import NotificationRuleService, RuleInput, RuleScope
from backend.services.secret_cipher import SecretCipher
from backend.worker.notification_handlers import build_notification_handlers
from backend.worker.rpc import RpcServer
from tests.api_support import FakeClock

ACTOR = Actor.system("test")
SLACK_URL = "https://hooks.slack.test/services/T000/B000/XXXXsecretXXXX"
WEBHOOK_URL = "https://hooks.example.test/in/abcd1234"
SIGNING_SECRET = "signing-secret-value"
SMTP_PASSWORD = "smtp-password-value"
ORIGIN = "https://jendock.example.test"


@dataclass(frozen=True, slots=True)
class PostCall:
    """One recorded HTTP request."""

    url: str
    body: bytes
    headers: Mapping[str, str]

    def json(self) -> dict[str, object]:
        """The body as JSON."""
        loaded = json.loads(self.body)
        assert isinstance(loaded, dict)
        return loaded


class FakePoster:
    """Records requests; answers 200 unless scripted otherwise (ints or exceptions)."""

    def __init__(self) -> None:
        self.calls: list[PostCall] = []
        self.script: list[int | Exception] = []

    def post(self, url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> int:
        """Record and answer."""
        self.calls.append(PostCall(url, body, dict(headers)))
        outcome = self.script.pop(0) if self.script else 200
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class FakeMailer:
    """Records messages; fails when ``error`` is set."""

    def __init__(self) -> None:
        self.sent: list[tuple[Mapping[str, object], EmailMessage]] = []
        self.error: DeliveryError | None = None

    def send(self, config: Mapping[str, object], message: EmailMessage, timeout: float) -> None:
        """Record or fail."""
        if self.error is not None:
            raise self.error
        self.sent.append((dict(config), message))


@dataclass(slots=True)
class World:
    """Real notification services on the test database with fake senders."""

    engine: Engine
    cipher: SecretCipher
    clock: FakeClock
    poster: FakePoster = field(default_factory=FakePoster)
    mailer: FakeMailer = field(default_factory=FakeMailer)
    dedupe_minutes: int = 30
    policy: DispatchPolicy = field(default_factory=DispatchPolicy)

    def uow(self) -> UnitOfWork:
        """A fresh unit of work."""
        return UnitOfWork(create_session_factory(self.engine))

    @property
    def deliverer(self) -> Deliverer:
        """The deliverer over the fakes."""
        return Deliverer(self.poster, self.mailer, ORIGIN, 5.0)

    def channels(self) -> ChannelService:
        """A channel service."""
        return ChannelService(self.uow(), self.cipher, self.deliverer, self.clock)

    def rules(self) -> NotificationRuleService:
        """A rule service."""
        return NotificationRuleService(self.uow(), self.clock)

    @property
    def emitter(self) -> OutboxEmitter:
        """The outbox emitter."""
        return OutboxEmitter(self.uow, self.dedupe_minutes, self.clock)

    @property
    def dispatcher(self) -> NotificationDispatcher:
        """The dispatcher (a new one per access: it keeps only prune bookkeeping)."""
        return NotificationDispatcher(
            self.uow, self.cipher, self.deliverer, self.policy, self.clock
        )

    def slack(self, name: str = "team-slack", url: str = SLACK_URL) -> int:
        """Create a Slack channel; returns its id."""
        return self.channels().create(name, ChannelKind.SLACK, {"webhook_url": url}, ACTOR).id

    def webhook(
        self, name: str = "hook", url: str = WEBHOOK_URL, signing: str | None = None
    ) -> int:
        """Create a generic webhook channel."""
        config: dict[str, object] = {"url": url}
        if signing:
            config["signing_secret"] = signing
        return self.channels().create(name, ChannelKind.WEBHOOK, config, ACTOR).id

    def email(self, name: str = "mail") -> int:
        """Create an SMTP channel with credentials."""
        config: dict[str, object] = {
            "host": "smtp.example.test",
            "port": 587,
            "security": "starttls",
            "username": "mailer",
            "password": SMTP_PASSWORD,
            "from_address": "jendock@example.test",
            "to_addresses": ["ops@example.test", "dev@example.test"],
        }
        return self.channels().create(name, ChannelKind.EMAIL, config, ACTOR).id

    def subscribe(
        self,
        channel_ids: list[int],
        events: list[NotificationEvent],
        scope: RuleScope | None = None,
    ) -> None:
        """Replace the rule set of ``scope`` (default: the global default) with one rule per
        channel carrying ``events``."""
        items = [RuleInput(cid, [e.value for e in events]) for cid in channel_ids]
        self.rules().replace(scope or RuleScope(), items, ACTOR)

    def deliveries(self) -> list[NotificationDelivery]:
        """All outbox rows, oldest first."""
        with self.uow() as uow:
            rows = NotificationRepository(uow.session).page(
                project_id=None, channel_id=None, status=None, limit=200, offset=0
            )[0]
            return sorted(rows, key=lambda row: row.id)


def payload(
    event: NotificationEvent = NotificationEvent.RUN_FAILED,
    key: str = "k1",
    **fields: object,
) -> EventPayload:
    """A payload for project ``demo`` / development."""
    base: dict[str, object] = {
        "event": event,
        "dedupe_key": f"{event.value}:{key}",
        "title": "Run #3 failed: demo/development",
        "project": "demo",
        "environment": EnvironmentName.DEVELOPMENT.value,
        "link_path": "/runs/3",
        "run_id": 3,
        "run_number": 3,
        "commit_sha": "0123456789abcdef0123456789abcdef01234567",
        "triggered_by": "admin (manual)",
        "failed_stage": "Build",
        "failed_step": "sh: make",
    }
    base.update(fields)
    return EventPayload(**base)  # type: ignore[arg-type]


@contextmanager
def running_notification_worker(
    socket_dir: Path, engine: Engine, key_file: Path, poster: FakePoster, mailer: FakeMailer
) -> Iterator[Path]:
    """A real RPC server (real cipher) exposing the ``notify.channel.*`` methods."""
    socket_path = socket_dir / "n.sock"
    cipher = SecretCipher(key_file)
    factory = create_session_factory(engine)
    deliverer = Deliverer(poster, mailer, ORIGIN, 5.0)

    def service() -> ChannelService:
        return ChannelService(UnitOfWork(factory), cipher, deliverer)

    server = RpcServer(socket_path, build_notification_handlers(service))
    server.start()
    try:
        yield socket_path
    finally:
        server.stop()


Clock = Callable[[], object]
