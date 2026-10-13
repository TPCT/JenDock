"""Events are emitted at the right points: runner, deployer, recovery, renewal, monitors."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine

from backend.adapters.acme.client import AcmeError
from backend.adapters.compose_deployer import ComposeDeployer, DeployConfig
from backend.adapters.deploy_journal import DeployJournal, JournalEntry
from backend.adapters.deploy_ledger import ServiceChange
from backend.adapters.errors import DeployError
from backend.config_notifications import NotificationSettings
from backend.domain.enums import DeploymentStatus, GitTransport, RunStatus
from backend.domain.notifications import EventPayload, NotificationEvent
from backend.execution.cancel import CancelReason, CancelToken
from backend.execution.recovery import recover_interrupted_runs
from backend.logging import SecretRedactor
from backend.repositories.database import UnitOfWork, create_session_factory
from backend.services.deployments import DatabaseLedger
from backend.services.notification_events import (
    CertificateNotifier,
    DeploymentNotifier,
    NotifyingLedger,
    RunNotifier,
    tail_of,
)
from backend.services.notification_monitors import DiskMonitor, DiskUsage, WebhookFailureTracker
from backend.services.notification_outbox import NoEvents
from backend.services.renewal import RenewalScheduler
from backend.services.secret_cipher import SecretCipher
from backend.worker.github_handlers import GithubHandlers
from backend.worker.notification_wiring import build_notification_parts
from backend.worker.rpc import RpcCall
from tests.api_support import FakeClock, make_settings
from tests.deploy_support import ManualClock
from tests.execution_support import Harness
from tests.ingress_support import HOST
from tests.ingress_world import IngressWorld
from tests.notification_support import World
from tests.test_deploy_units import POLICY, Sim, build_sim
from tests.test_pipeline_runner import pipeline, stage

E = NotificationEvent
ALL_RUN_EVENTS = [E.RUN_FAILED, E.RUN_FIXED, E.RUN_INTERRUPTED]


class Recorder:
    """An ``EventSink`` that keeps what was emitted."""

    def __init__(self) -> None:
        self.events: list[EventPayload] = []

    def emit(self, payload: EventPayload) -> int:
        """Record."""
        self.events.append(payload)
        return 1


@pytest.fixture
def world(engine: Engine, cipher: SecretCipher, clock: FakeClock, project: str) -> World:
    return World(engine, cipher, clock)


@pytest.fixture
def wired(harness: Harness, world: World) -> Harness:
    """The execution harness with run notifications wired to the real outbox."""
    redactor = SecretRedactor()
    notifier = RunNotifier(world.uow, harness.env.logs, world.emitter, redactor)
    harness.env = replace(harness.env, notifications=notifier)
    channel = world.slack()
    world.subscribe([channel], ALL_RUN_EVENTS)
    return harness


def queued(world: World) -> list[tuple[str, dict[str, object]]]:
    return [(row.event, row.payload) for row in world.deliveries()]


# --- runs ---------------------------------------------------------------------------------------


def test_a_failed_run_announces_the_failing_stage_step_and_the_last_20_masked_log_lines(
    wired: Harness, world: World
) -> None:
    secret = "s3cr3t-value-123"
    wired.add_secret("API_TOKEN", secret)
    lines = (*(f"output line {n}" for n in range(30)), f"token is {secret}")
    wired.runtime.when("build", lines=lines, exit_code=2)
    head = "environment {\nTOKEN = credentials('API_TOKEN')\n}"
    text = pipeline(stage("Lint", "sh 'lint ok'") + stage("Build", "sh 'build $TOKEN'"), head=head)
    run, outcome = wired.run(text)
    assert outcome.status is RunStatus.FAILED

    [(event, stored)] = queued(world)
    assert event == "run.failed"
    assert stored["title"] == f"Run #{run.number} failed: demo/development"
    assert stored["failed_stage"] == "Build" and stored["failed_step"] == "sh: build $TOKEN"
    assert stored["commit_sha"] == "0123456"  # short sha
    assert stored["triggered_by"] == "manual"
    assert stored["link_path"] == f"/runs/{run.id}"
    tail = stored["log_tail"]
    assert isinstance(tail, list) and len(tail) == 20
    assert tail[-2] == "token is [REDACTED]"
    assert tail[-1].startswith("ERROR: script exited")
    assert tail[0] == "output line 12"
    assert secret not in json.dumps(stored)

    world.dispatcher.run_once()
    [call] = world.poster.calls
    body = call.body.decode()
    assert f"/runs/{run.id}|Open in Jendock" in body and secret not in body


def test_a_run_that_succeeds_after_a_failure_is_announced_as_fixed_once(
    wired: Harness, world: World, clock: FakeClock
) -> None:
    wired.runtime.when("bad", exit_code=1)
    assert wired.run(pipeline(stage("A", "sh 'ok'")))[1].status is RunStatus.SUCCEEDED
    assert queued(world) == []  # a success after a success is not news
    assert wired.run(pipeline(stage("A", "sh 'bad'")))[1].status is RunStatus.FAILED
    clock.advance(minutes=1)
    second, outcome = wired.run(pipeline(stage("A", "sh 'ok'")))
    assert outcome.status is RunStatus.SUCCEEDED
    assert [event for event, _ in queued(world)] == ["run.failed", "run.fixed"]
    fixed = queued(world)[1][1]
    assert fixed["title"] == f"Run #{second.number} succeeded again: demo/development"
    assert fixed["log_tail"] == [] and fixed["failed_stage"] is None
    wired.run(pipeline(stage("A", "sh 'ok'")))
    assert len(queued(world)) == 2  # fixed only follows a failure


def test_cancelled_runs_are_not_announced(wired: Harness, world: World) -> None:
    run = wired.queue(pipeline(stage("A", "sh 'work'")))
    token = CancelToken()
    token.cancel(CancelReason.CANCELLED)
    wired.execute(run, token)
    assert queued(world) == []


def test_a_run_that_fails_before_any_stage_still_names_where_it_failed(
    wired: Harness, world: World
) -> None:
    run, outcome = wired.run("this is not a pipeline")
    assert outcome.status is RunStatus.FAILED
    [(event, stored)] = queued(world)
    assert event == "run.failed" and stored["failed_stage"] == "Pipeline"
    assert stored["message"] is not None and "syntax error" in str(stored["message"])
    assert run.id == stored["run_id"]


def test_repeated_failures_of_the_same_environment_collapse(
    wired: Harness, world: World, clock: FakeClock
) -> None:
    wired.runtime.when("bad", exit_code=1)
    wired.run(pipeline(stage("A", "sh 'bad'")))
    clock.advance(minutes=5)
    wired.run(pipeline(stage("A", "sh 'bad'")))
    assert len(queued(world)) == 1
    assert world.deliveries()[0].suppressed == 1


def test_crash_recovery_announces_interrupted_runs(wired: Harness, world: World) -> None:
    crashed = wired.queue(pipeline(stage("S", "sh 'x'")))
    assert wired.env.lifecycle().claim_next() is not None
    assert recover_interrupted_runs(wired.env) == [crashed.id]
    [(event, stored)] = queued(world)
    assert event == "run.interrupted" and stored["run_id"] == crashed.id
    assert "interrupted" in str(stored["title"])


def test_a_notification_failure_never_fails_the_run(
    wired: Harness, world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("outbox down")

    monkeypatch.setattr(RunNotifier, "_build", broken)
    wired.runtime.when("bad", exit_code=1)
    _, outcome = wired.run(pipeline(stage("A", "sh 'bad'")))
    assert outcome.status is RunStatus.FAILED
    assert queued(world) == []


def test_the_default_environment_has_notifications_off(harness: Harness) -> None:
    run, outcome = harness.run(pipeline(stage("A", "sh 'ok'")))
    assert outcome.status is RunStatus.SUCCEEDED and run.id


def test_log_tail_strips_escapes_masks_again_and_keeps_the_last_20_lines() -> None:
    redactor = SecretRedactor()
    redactor.register("hunter2hunter2")
    raw = ["\x1b[31mred\x1b[0m", "", "pw=hunter2hunter2", *[f"l{n}" for n in range(30)]]
    tail = tail_of(raw, redactor)
    assert len(tail) == 20 and tail[-1] == "l29"
    assert tail_of(["\x1b[31mred\x1b[0m", "pw=hunter2hunter2"], redactor) == (
        "red",
        "pw=[REDACTED]",
    )
    assert tail_of(["x" * 500], None)[0] == "x" * 240


# --- deployments --------------------------------------------------------------------------------


def _notified_sim(tmp_path: Path) -> tuple[Sim, Recorder, list[int]]:
    sim = build_sim(tmp_path)
    seen: list[int] = []

    class Notifier:
        def deployment_finished(self, deployment_id: int) -> None:
            seen.append(deployment_id)

    ledger = NotifyingLedger(sim.ledger, Notifier())  # type: ignore[arg-type]
    sim.deployer = ComposeDeployer(
        sim.docker,
        ledger,
        sim.root,
        sim.probe,
        config=DeployConfig(health=POLICY),
        clock=ManualClock(),
    )
    return sim, Recorder(), seen


def test_the_deployer_announces_each_final_status_exactly_once(tmp_path: Path) -> None:
    sim, _, seen = _notified_sim(tmp_path)
    good = sim.new_image("jendock/demo-development-web:aaa-r2")
    sim.deploy(sim.request(("web", good)))
    assert seen == [1] and sim.ledger.rows[1].status is DeploymentStatus.SUCCEEDED

    bad = sim.new_image("jendock/demo-development-web:bbb-r3")
    sim.docker.behaviour[bad] = "unhealthy"
    with pytest.raises(DeployError, match="rolled back"):
        sim.deploy(sim.request(("web", bad)))
    assert seen == [1, 2] and sim.ledger.rows[2].status is DeploymentStatus.ROLLED_BACK

    worse = sim.new_image("jendock/demo-development-web:ccc-r4")
    sim.docker.behaviour[worse] = "unhealthy"
    sim.docker.behaviour[good] = "unhealthy"
    with pytest.raises(DeployError, match="ROLLBACK FAILED"):
        sim.deploy(sim.request(("web", worse)))
    assert seen == [1, 2, 3] and sim.ledger.rows[3].status is DeploymentStatus.ROLLBACK_FAILED


def test_crash_recovery_of_a_deployment_is_announced_through_the_same_hook(
    tmp_path: Path,
) -> None:

    sim, _, seen = _notified_sim(tmp_path)
    new = sim.new_image()
    deployment_id = sim.ledger.begin("demo", "development", 7, "a" * 40)
    changes = (ServiceChange("web", new, sim.docker.image_id(new), "app-web:1", sim.old_web),)
    sim.ledger.record_services(deployment_id, changes)
    entry = JournalEntry(
        deployment_id, 7, "demo", "development", "demo-dev", sim.files, {}, changes, sim.env_files
    )
    DeployJournal(sim.root).write(entry)
    sim.docker.start("web", new, "healthy")
    assert sim.deployer.recover() == [deployment_id]
    assert seen == [deployment_id]  # rolled back by recovery


def test_deployment_events_carry_status_severity_trigger_and_link(
    wired: Harness, world: World
) -> None:
    run = wired.queue(pipeline(stage("S", "sh 'x'")))
    ledger = NotifyingLedger(
        DatabaseLedger(world.uow), DeploymentNotifier(world.uow, world.emitter)
    )
    channel = world.slack("deploys")
    world.subscribe(
        [channel],
        [
            E.DEPLOYMENT_SUCCEEDED,
            E.DEPLOYMENT_ROLLED_BACK,
            E.DEPLOYMENT_ROLLBACK_FAILED,
        ],
    )

    ids = []
    for status, message in (
        (DeploymentStatus.SUCCEEDED, None),
        (DeploymentStatus.ROLLED_BACK, "deployment failed (unhealthy); rolled back"),
        (DeploymentStatus.ROLLBACK_FAILED, "ROLLBACK FAILED, manual intervention needed"),
        (DeploymentStatus.FAILED, "nothing changed"),
    ):
        deployment_id = ledger.begin("demo", "development", run.id, "a" * 40)
        ledger.record_services(deployment_id, [ServiceChange("web", "img:1")])
        ledger.finish(deployment_id, status, message)
        ids.append(deployment_id)
    rows = [(e, p) for e, p in queued(world) if e.startswith("deployment.")]
    assert [event for event, _ in rows] == [
        "deployment.succeeded",
        "deployment.rolled_back",
        "deployment.rollback_failed",
    ]  # a deployment that changed nothing (failed) is not announced
    succeeded, rolled_back, failed = (p for _, p in rows)
    assert succeeded["title"] == "Deployment succeeded: demo/development"
    assert succeeded["message"] is None and succeeded["details"] == {"Services": "web"}
    assert rolled_back["message"] == "deployment failed (unhealthy); rolled back"
    assert "manual intervention" in str(failed["message"])
    assert failed["deployment_id"] == ids[2] and failed["link_path"] == f"/runs/{run.id}"
    assert failed["commit_sha"] == "aaaaaaa" and failed["run_number"] == run.number
    assert EventPayload.from_json(failed).severity.value == "critical"


# --- certificates -------------------------------------------------------------------------------


@pytest.fixture
def ingress(engine: Engine, clock: FakeClock, project: str) -> IngressWorld:

    factory = create_session_factory(engine)
    return IngressWorld(lambda: UnitOfWork(factory), clock)


def _renewal(ingress: IngressWorld, sink: Recorder) -> RenewalScheduler:
    notifier = CertificateNotifier(ingress.uow_factory, sink)
    return RenewalScheduler(ingress.certificates(), interval=3600, notifier=notifier)


def test_a_certificate_with_less_than_14_days_left_is_announced(
    ingress: IngressWorld, clock: FakeClock
) -> None:
    sink = Recorder()
    domain = ingress.create()
    ingress.apply(domain)
    clock.advance(days=40)  # 50 days left: nothing to say
    _renewal(ingress, sink).tick()
    assert sink.events == []
    ingress.issuer.fail_with = AcmeError("CA down")  # renewals fail from here on
    clock.advance(days=40)  # 10 days left
    clock.advance(hours=7)
    _renewal(ingress, sink).tick()
    kinds = sorted(e.event.value for e in sink.events)
    assert kinds == ["certificate.expiring", "certificate.renewal_failed"]
    expiring = next(e for e in sink.events if e.event is E.CERTIFICATE_EXPIRING)
    assert expiring.project == "demo" and expiring.environment == "development"
    assert expiring.details["Hostname"] == HOST and expiring.details["Days left"] == "9"
    assert expiring.link_path == "/projects/demo/environments/development/domains"
    failed = next(e for e in sink.events if e.event is E.CERTIFICATE_RENEWAL_FAILED)
    assert "CA down" in (failed.message or "") and HOST in failed.title


def test_skipped_renewals_are_not_failures(ingress: IngressWorld, clock: FakeClock) -> None:
    sink = Recorder()
    domain = ingress.create()
    ingress.apply(domain)
    ingress.domains().record_error(domain.id, "apply failed")
    clock.advance(days=61)
    _renewal(ingress, sink).tick()
    assert [e.event for e in sink.events] == []


# --- monitors -----------------------------------------------------------------------------------


def test_disk_monitor_announces_usage_above_the_threshold() -> None:
    sink = Recorder()
    usage = {"value": DiskUsage(100 * 1024**3, 80 * 1024**3, 20 * 1024**3)}
    monitor = DiskMonitor(Path("/data"), 90, sink, 300, lambda _path: usage["value"])
    assert monitor.tick() is False and sink.events == []
    usage["value"] = DiskUsage(100 * 1024**3, 93 * 1024**3, 7 * 1024**3)
    assert monitor.tick() is True
    [event] = sink.events
    assert event.event is E.DISK_USAGE_HIGH and event.project is None
    assert event.details == {"Used": "93%", "Free": "7.0 GiB", "Threshold": "90%"}


def test_webhook_failures_announce_only_a_spike(clock: FakeClock) -> None:
    sink = Recorder()
    tracker = WebhookFailureTracker(sink, threshold=3, window=timedelta(minutes=10), clock=clock)
    for _ in range(3):
        tracker.record_failure()
        clock.advance(minutes=1)
    assert sink.events == []  # three is not more than three
    tracker.record_failure()
    [event] = sink.events
    assert event.event is E.WEBHOOK_SIGNATURE_SPIKE and event.details["Failures"] == "4"
    clock.advance(minutes=30)  # the window has passed: the counter starts again
    tracker.record_failure()
    assert len(sink.events) == 1


def test_an_invalid_webhook_signature_is_counted_by_the_rpc_handler(
    tmp_path: Path, clock: FakeClock
) -> None:
    sink = Recorder()
    tracker = WebhookFailureTracker(sink, 1, timedelta(minutes=10), clock)
    secret_file = tmp_path / "webhook-secret"
    secret_file.write_text("webhook-secret")

    class NoHeads:
        def head(self, *_args: object, **_kwargs: object) -> None:
            return None

    handlers = GithubHandlers(
        secret_file,
        NoHeads(),
        lambda _slug: ("o/r", GitTransport.HTTPS),
        tracker.record_failure,
    )
    bad = RpcCall(None, {"signature": "sha256=" + "0" * 64, "body_b64": "e30="})
    assert handlers.verify_webhook(bad) == {"valid": False}
    assert handlers.verify_webhook(bad) == {"valid": False}
    assert [e.event for e in sink.events] == [E.WEBHOOK_SIGNATURE_SPIKE]


# --- wiring -------------------------------------------------------------------------------------


def test_the_worker_wires_every_emission_point(
    tmp_path: Path, engine: Engine, cipher: SecretCipher
) -> None:

    settings = make_settings(tmp_path)
    parts = build_notification_parts(
        settings, create_session_factory(engine), cipher, SecretRedactor()
    )
    assert {"notify.channel.create", "notify.channel.test"} <= set(parts.handlers)
    assert parts.dispatcher is not None and parts.disk is not None
    off = build_notification_parts(
        settings,
        create_session_factory(engine),
        cipher,
        SecretRedactor(),
        notify=NotificationSettings(enabled=False),
    )
    assert isinstance(off.sink, NoEvents) and off.dispatcher is None and off.disk is None
    assert off.sink.emit(EventPayload(E.RUN_FAILED, "k", "t")) == 0
