from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from subprocess import CompletedProcess

import pytest
from sqlalchemy import Engine

from backend.adapters.container_runtime import CancelSignal, ExecResult, LineSink
from backend.adapters.override_store import OverrideStore
from backend.containers.base import ContainerAuditor, TargetResolver
from backend.containers.power import EnvironmentPower, EnvironmentPowerService, PowerState
from backend.domain.actor import Actor
from backend.domain.enums import ActorType, EnvironmentName
from backend.domain.errors import ConflictError
from backend.repositories.database import UnitOfWork, create_session_factory
from backend.services.audit import AuditService
from backend.services.auto_stop import AutoStopScheduler
from backend.services.environments import EnvironmentService, EnvironmentUpdate
from tests.container_harness import DEV, Stack, build_stack


class Commands:
    def __init__(self) -> None:
        self.args: list[str] = []
        self.returncode = 0

    def capture(self, args: Sequence[str], timeout: float = 60) -> CompletedProcess[str]:
        raise AssertionError("capture is not expected")

    def stream(
        self,
        args: Sequence[str],
        sink: LineSink,
        *,
        timeout: float | None,
        cancel: CancelSignal | None = None,
    ) -> ExecResult:
        self.args = list(args)
        return ExecResult(self.returncode)


def _power(tmp_path: Path, db: Engine, stack: Stack, commands: Commands) -> EnvironmentPowerService:
    factory = create_session_factory(db)
    target = TargetResolver(factory)
    image_store = OverrideStore(tmp_path / "deployments")
    image = image_store.write("demo-dev", {"web": "web:released"})
    managed = tmp_path / "resource.yml"
    managed.write_text("services: {}\n")
    return EnvironmentPowerService(
        target,
        stack.services.inventory,
        commands,
        lambda _resolved: [str(managed), str(image)],
        stack.locks,
        ContainerAuditor(factory),
        factory,
    )


def test_state_counts_running_and_total(tmp_path: Path, engine: Engine, project: str) -> None:
    stack = build_stack(tmp_path, engine, project)
    power = _power(tmp_path, engine, stack, Commands())
    current = power.state(project, DEV)
    assert current.state is PowerState.RUNNING
    assert current.running == 1
    assert current.total == 2


def test_stop_uses_managed_files_and_image_override_last(
    tmp_path: Path, engine: Engine, project: str
) -> None:
    stack = build_stack(tmp_path, engine, project)
    commands = Commands()
    power = _power(tmp_path, engine, stack, commands)
    power.stop(project, DEV, Actor.system("test"))
    args = commands.args
    files = [args[index + 1] for index, item in enumerate(args[:-1]) if item == "-f"]
    assert files[-2].endswith("resource.yml")
    assert files[-1].endswith("override.yml")
    assert args[-1] == "stop"
    assert "down" not in args
    assert AuditService(UnitOfWork(create_session_factory(engine))).list(action="environment.stop")


def test_start_only_starts_services_with_containers_without_building_or_pulling(
    tmp_path: Path, engine: Engine, project: str
) -> None:
    stack = build_stack(tmp_path, engine, project)
    commands = Commands()
    _power(tmp_path, engine, stack, commands).start(project, DEV, Actor.system("test"))
    assert commands.args[-6:] == ["--detach", "--no-build", "--pull", "never", "web", "worker"]
    assert "--profile" not in commands.args
    files = [
        commands.args[index + 1] for index, item in enumerate(commands.args[:-1]) if item == "-f"
    ]
    assert files[-1].endswith("override.yml")


def test_start_rejects_not_deployed(tmp_path: Path, engine: Engine, project: str) -> None:
    stack = build_stack(tmp_path, engine, project)
    stack.engine.records.clear()
    power = _power(tmp_path, engine, stack, Commands())
    with pytest.raises(ConflictError, match="deploy it first"):
        power.start(project, DEV, Actor.system("test"))


def test_power_audits_done_and_failed_outcomes_with_the_actor(
    tmp_path: Path, engine: Engine, project: str
) -> None:
    stack = build_stack(tmp_path, engine, project)
    commands = Commands()
    power = _power(tmp_path, engine, stack, commands)
    actor = Actor(ActorType.USER, "77")

    power.stop(project, DEV, actor)
    power.start(project, DEV, actor)
    commands.returncode = 17
    with pytest.raises(ConflictError, match="could not change"):
        power.stop(project, DEV, actor)

    audit = AuditService(UnitOfWork(create_session_factory(engine)))
    stopped = audit.list(action="environment.stop")
    started = audit.list(action="environment.start")
    assert [event.details["outcome"] for event in stopped] == ["failed", "done"]
    assert started[0].details["outcome"] == "done"
    for event in (*stopped, *started):
        assert event.actor_type is ActorType.USER
        assert event.actor_id == "77"


def test_auto_stop_tick_waits_until_due_and_uses_system_actor(engine: Engine, project: str) -> None:
    factory = create_session_factory(engine)
    EnvironmentService(UnitOfWork(factory)).update(
        project, DEV, EnvironmentUpdate(auto_stop_hours=2, set_auto_stop_hours=True)
    )

    class FakePower:
        state_value = EnvironmentPower(
            PowerState.RUNNING,
            1,
            1,
            2,
            datetime(2026, 10, 16, 8, tzinfo=UTC),
            datetime(2026, 10, 16, 9, tzinfo=UTC),
            datetime(2026, 10, 16, 11, tzinfo=UTC),
        )

        def __init__(self) -> None:
            self.stopped: list[tuple[str, Actor, dict[str, object]]] = []

        def state(self, slug: str, env: EnvironmentName) -> EnvironmentPower:
            return self.state_value

        def stop(
            self,
            slug: str,
            env: EnvironmentName,
            actor: Actor,
            *,
            audit_action: str,
            audit_details: dict[str, object] | None,
            only_if: Callable[[EnvironmentPower], bool] | None = None,
        ) -> None:
            if only_if is not None and not only_if(self.state_value):
                return
            self.stopped.append((slug, actor, {**(audit_details or {}), "action": audit_action}))

    power = FakePower()
    scheduler = AutoStopScheduler(factory, power)  # type: ignore[arg-type]
    scheduler.tick(datetime(2026, 10, 16, 10, 59, tzinfo=UTC))
    assert power.stopped == []
    power.state_value = EnvironmentPower(
        PowerState.RUNNING,
        1,
        1,
        2,
        datetime(2026, 10, 16, 8, tzinfo=UTC),
        datetime(2026, 10, 16, 9, 30, tzinfo=UTC),
        datetime(2026, 10, 16, 11, 30, tzinfo=UTC),
    )
    scheduler.tick(datetime(2026, 10, 16, 11, 29, tzinfo=UTC))
    assert power.stopped == []
    scheduler.tick(datetime(2026, 10, 16, 11, 30, tzinfo=UTC))
    assert len(power.stopped) == 1
    slug, actor, details = power.stopped[0]
    assert slug == project
    assert actor == Actor.system("auto-stop")
    assert details == {
        "hours": 2,
        "reference_time": "2026-10-16T09:30:00+00:00",
        "action": "environment.auto_stop",
    }


@pytest.mark.parametrize(
    "power_state",
    [
        EnvironmentPower(PowerState.STOPPED, 0, 1, 1, None, None, datetime(2026, 1, 1, tzinfo=UTC)),
        EnvironmentPower(PowerState.RUNNING, 1, 1, None, None, None, None),
    ],
    ids=["stopped", "auto-stop-off"],
)
def test_auto_stop_skips_stopped_and_disabled_environments(
    engine: Engine, project: str, power_state: EnvironmentPower
) -> None:
    factory = create_session_factory(engine)
    EnvironmentService(UnitOfWork(factory)).update(
        project, DEV, EnvironmentUpdate(auto_stop_hours=1, set_auto_stop_hours=True)
    )

    class FakePower:
        def state(self, _slug: str, _env: EnvironmentName) -> EnvironmentPower:
            return power_state

        def stop(
            self,
            *_args: object,
            only_if: Callable[[EnvironmentPower], bool] | None = None,
            **_kwargs: object,
        ) -> None:
            pytest.fail("ineligible environment was stopped")

    AutoStopScheduler(factory, FakePower()).tick(  # type: ignore[arg-type]
        datetime(2026, 10, 16, 12, tzinfo=UTC)
    )


def test_auto_stop_records_a_system_audit_row(tmp_path: Path, engine: Engine, project: str) -> None:
    stack = build_stack(tmp_path, engine, project)
    factory = create_session_factory(engine)
    EnvironmentService(UnitOfWork(factory)).update(
        project, DEV, EnvironmentUpdate(auto_stop_hours=1, set_auto_stop_hours=True)
    )
    with UnitOfWork(factory) as uow:
        environment = uow.environments.get(stack.environment_id)
        assert environment is not None
        environment.started_at = datetime(2026, 10, 16, 9, tzinfo=UTC)
    power = _power(tmp_path, engine, stack, Commands())

    AutoStopScheduler(factory, power).tick(datetime(2026, 10, 16, 10, tzinfo=UTC))

    [event] = AuditService(UnitOfWork(factory)).list(action="environment.auto_stop")
    assert event.actor_type is ActorType.SYSTEM
    assert event.actor_id == "auto-stop"
    assert event.details["outcome"] == "done"
    assert event.details["hours"] == 1
    assert event.details["reference_time"] == "2026-10-16T09:00:00+00:00"


def test_auto_stop_candidates_are_development_only(engine: Engine, project: str) -> None:
    factory = create_session_factory(engine)
    EnvironmentService(UnitOfWork(factory)).update(
        project, DEV, EnvironmentUpdate(auto_stop_hours=1, set_auto_stop_hours=True)
    )
    with UnitOfWork(factory) as uow:
        production = uow.environments.get_by_project_slug(project, EnvironmentName.PRODUCTION)
        assert production is not None
        production.auto_stop_hours = 1

    class FakePower:
        def __init__(self) -> None:
            self.states: list[EnvironmentName] = []
            self.stopped: list[EnvironmentName] = []

        def state(self, _slug: str, env: EnvironmentName) -> EnvironmentPower:
            self.states.append(env)
            return EnvironmentPower(
                PowerState.RUNNING,
                1,
                1,
                1,
                None,
                datetime(2026, 10, 16, 9, tzinfo=UTC),
                datetime(2026, 10, 16, 10, tzinfo=UTC),
            )

        def stop(
            self,
            _slug: str,
            env: EnvironmentName,
            *_args: object,
            only_if: Callable[[EnvironmentPower], bool] | None = None,
            **_kwargs: object,
        ) -> None:
            self.stopped.append(env)

    power = FakePower()
    AutoStopScheduler(factory, power).tick(datetime(2026, 10, 16, 10, tzinfo=UTC))  # type: ignore[arg-type]
    assert power.states == [DEV]
    assert power.stopped == [DEV]


def test_auto_stop_rechecks_deadline_after_acquiring_environment_lock(
    tmp_path: Path, engine: Engine, project: str
) -> None:
    stack = build_stack(tmp_path, engine, project)
    factory = create_session_factory(engine)
    EnvironmentService(UnitOfWork(factory)).update(
        project, DEV, EnvironmentUpdate(auto_stop_hours=1, set_auto_stop_hours=True)
    )
    with UnitOfWork(factory) as uow:
        environment = uow.environments.get(stack.environment_id)
        assert environment is not None
        environment.started_at = datetime(2026, 10, 16, 9, tzinfo=UTC)

    commands = Commands()
    power = _power(tmp_path, engine, stack, commands)
    original_locks = stack.locks

    class DeadlineMovesAfterLock:
        @contextmanager
        def hold(self, key: str, *, timeout: float | None) -> Iterator[None]:
            # Model a deployment completing after the scheduler scan and before this stop
            # operation acquires the shared environment lock.
            with UnitOfWork(factory) as uow:
                environment = uow.environments.get(stack.environment_id)
                assert environment is not None
                environment.started_at = datetime(2026, 10, 16, 9, 30, tzinfo=UTC)
            with original_locks.hold(key, timeout=timeout):
                yield

    power._locks = DeadlineMovesAfterLock()  # type: ignore[assignment]
    AutoStopScheduler(factory, power).tick(datetime(2026, 10, 16, 10, tzinfo=UTC))

    assert commands.args == []
    assert AuditService(UnitOfWork(factory)).list(action="environment.auto_stop") == []
