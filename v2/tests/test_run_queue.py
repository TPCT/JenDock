"""Run queue, cancellation, shutdown and crash recovery."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from sqlalchemy import Engine

from shiftcodes.domain.actor import Actor
from shiftcodes.domain.enums import RunStatus
from shiftcodes.domain.errors import ConflictError, NotFoundError
from shiftcodes.execution.config import ExecutionConfig
from shiftcodes.execution.recovery import recover_interrupted_runs
from shiftcodes.execution.scheduler import RunScheduler
from shiftcodes.services.audit import AuditService
from shiftcodes.services.run_lifecycle import CancelOutcome
from shiftcodes.services.secret_cipher import SecretCipher
from tests.execution_support import Harness, make_harness
from tests.test_pipeline_runner import pipeline, stage

ACTOR = Actor.system("test")
OK = RunStatus.SUCCEEDED


def wait_for(condition: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "condition not reached in time"
        time.sleep(0.01)


def _status(harness: Harness, run_id: int) -> RunStatus:
    with harness.uow() as uow:
        run = uow.runs.get(run_id)
        assert run is not None
        return run.status


def _local(
    tmp_path: Path, engine: Engine, cipher: SecretCipher, project: str, **kw: object
) -> Harness:
    config = ExecutionConfig(health_interval=0.01, cancel_poll_interval=0.05, **kw)  # type: ignore[arg-type]
    return make_harness(tmp_path, engine, cipher, project, config=config)


def test_queue_claims_oldest_first_up_to_the_concurrency_limit(harness: Harness) -> None:
    harness.runtime.when("hold", block=True)
    runs = [harness.queue(pipeline(stage("S", f"sh 'hold {n}'"))) for n in range(3)]
    scheduler = RunScheduler(harness.env)
    assert scheduler.tick() == [runs[0].id]  # default limit is 1
    wait_for(lambda: len(harness.runtime.running) == 1)
    assert scheduler.tick() == []  # still busy
    assert _status(harness, runs[1].id) is RunStatus.QUEUED
    harness.env.lifecycle().request_cancel(runs[0].id, ACTOR)
    assert scheduler.wait_idle(10)
    assert scheduler.tick() == [runs[1].id]
    harness.env.lifecycle().request_cancel(runs[1].id, ACTOR)
    assert scheduler.wait_idle(10)
    assert scheduler.tick() == [runs[2].id]
    harness.env.lifecycle().request_cancel(runs[2].id, ACTOR)
    assert scheduler.wait_idle(10)
    assert [_status(harness, r.id) for r in runs] == [RunStatus.CANCELLED] * 3


def test_concurrency_limit_is_configurable(
    tmp_path: Path, engine: Engine, cipher: SecretCipher, project: str
) -> None:
    local = _local(tmp_path, engine, cipher, project, max_concurrent_runs=2)
    local.runtime.when("slow", delay=0.3)
    runs = [local.queue(pipeline(stage("S", f"sh 'slow {n}'"))) for n in range(3)]
    scheduler = RunScheduler(local.env)
    assert scheduler.tick() == [runs[0].id, runs[1].id]
    wait_for(lambda: scheduler.wait_idle(0.05))
    assert local.runtime.max_concurrent == 2
    assert scheduler.tick() == [runs[2].id]
    assert scheduler.wait_idle(10)
    assert [_status(local, r.id) for r in runs] == [OK, OK, OK]


def test_a_finished_queue_is_idle(harness: Harness) -> None:
    assert RunScheduler(harness.env).tick() == []


def test_cancel_queued_run_is_immediate_and_audited(harness: Harness) -> None:
    run = harness.queue(pipeline(stage("S", "sh 'x'")))
    outcome = harness.env.lifecycle().request_cancel(run.id, ACTOR)
    assert outcome is CancelOutcome.CANCELLED
    with harness.uow() as uow:
        stored = uow.runs.get(run.id)
        assert stored is not None
        assert stored.status is RunStatus.CANCELLED
        assert stored.finished_at is not None
        assert stored.message == "cancelled before it started"
    assert RunScheduler(harness.env).tick() == []  # never claimed
    events = AuditService(harness.uow()).list(action="run.cancel")
    assert [(e.target_id, e.details["outcome"]) for e in events] == [(str(run.id), "cancelled")]


def test_cancel_finished_or_unknown_run(harness: Harness) -> None:
    run, _ = harness.run(pipeline(stage("S", "sh 'x'")))
    with pytest.raises(ConflictError):
        harness.env.lifecycle().request_cancel(run.id, ACTOR)
    with pytest.raises(NotFoundError):
        harness.env.lifecycle().request_cancel(9999, ACTOR)


def test_cancel_running_sets_the_request_and_the_worker_finishes_it(harness: Harness) -> None:
    harness.runtime.when("hold", block=True)
    run = harness.queue(pipeline(stage("S", "sh 'hold'")))
    scheduler = RunScheduler(harness.env)
    scheduler.tick()
    wait_for(lambda: bool(harness.runtime.running))
    outcome = harness.env.lifecycle().request_cancel(run.id, ACTOR)
    assert outcome is CancelOutcome.REQUESTED
    assert scheduler.wait_idle(10)
    assert _status(harness, run.id) is RunStatus.CANCELLED
    events = AuditService(harness.uow()).list(action="run.cancel")
    assert events[0].details["outcome"] == "requested"


def test_shutdown_interrupts_running_runs_and_skips_post(harness: Harness) -> None:
    harness.runtime.when("hold", block=True)
    post = "post {\nalways { echo 'post' }\n}"
    run = harness.queue(pipeline(stage("S", "sh 'hold'"), post=post))
    scheduler = RunScheduler(harness.env)
    stop = threading.Event()

    class Stop:
        def wait(self, timeout: float) -> bool:
            return stop.wait(timeout)

    thread = threading.Thread(target=scheduler.run_forever, args=(Stop(),))
    thread.start()
    wait_for(lambda: bool(harness.runtime.running))
    stop.set()
    thread.join(15)
    assert not thread.is_alive()
    with harness.uow() as uow:
        stored = uow.runs.get(run.id)
        assert stored is not None
        assert stored.status is RunStatus.INTERRUPTED
        assert stored.message is not None
        assert "shut down" in stored.message
    assert "post" not in harness.runtime.script_order
    assert not harness.env.workspaces.path(run.id).exists()


# --- recovery ----------------------------------------------------------------------------------


def test_recovery_interrupts_running_runs_kills_containers_and_removes_workspaces(
    harness: Harness,
) -> None:
    crashed = harness.queue(pipeline(stage("S", "sh 'x'")))
    queued = harness.queue(pipeline(stage("S", "sh 'x'")))
    assert harness.env.lifecycle().claim_next() is not None  # crashed is now "running"
    recorder = harness.env.recorder()
    stage_ids = recorder.plan_stages(crashed.id, ["Checkout", "Build", "Deploy"])
    recorder.start_stage(stage_ids[0])
    recorder.finish_stage(stage_ids[0], OK)
    recorder.start_stage(stage_ids[1])
    step = recorder.start_step(stage_ids[1], "sh", "make")
    leftover = harness.env.workspaces.create(crashed.id)
    (leftover / "file").write_text("x")
    orphan = harness.env.workspaces.create(777)

    assert recover_interrupted_runs(harness.env) == [crashed.id]

    with harness.uow() as uow:
        stored = uow.runs.get(crashed.id)
        assert stored is not None
        assert stored.status is RunStatus.INTERRUPTED
        assert stored.finished_at is not None
        assert stored.message is not None
        assert "worker stopped or restarted" in stored.message
        assert [s.status for s in uow.runs.list_stages(crashed.id)] == [
            OK,
            RunStatus.INTERRUPTED,
            RunStatus.SKIPPED,
        ]
        restored = uow.runs.get_step(step)
        assert restored is not None
        assert restored.status is RunStatus.INTERRUPTED
        assert restored.finished_at is not None
    assert _status(harness, queued.id) is RunStatus.QUEUED
    assert f"shiftcodes.run_id={crashed.id}" in harness.runtime.kills
    assert not leftover.exists()
    assert not orphan.exists()


def test_recovery_is_idempotent(harness: Harness) -> None:
    run = harness.queue(pipeline(stage("S", "sh 'x'")))
    harness.env.lifecycle().claim_next()
    assert recover_interrupted_runs(harness.env) == [run.id]
    kills = list(harness.runtime.kills)
    assert recover_interrupted_runs(harness.env) == []
    assert harness.runtime.kills == kills
    assert _status(harness, run.id) is RunStatus.INTERRUPTED
