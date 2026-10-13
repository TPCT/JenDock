"""Container management use cases on fake Docker: identity, lifecycle, env, limits, files, logs."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from sqlalchemy import Engine

from shiftcodes.adapters.container_admin import FileRead
from shiftcodes.adapters.container_runtime import CancelSignal, LineSink
from shiftcodes.adapters.deployer import DeployRequest
from shiftcodes.containers.locks import EnvironmentBusyError, EnvironmentLocks, LockedDeployer
from shiftcodes.containers.logs import LogCursor
from shiftcodes.domain.actor import Actor
from shiftcodes.domain.containers import EnvChange, EnvironmentKey, LifecycleAction, LimitChange
from shiftcodes.domain.enums import ActorType, EnvironmentName
from shiftcodes.domain.errors import ConflictError, NotFoundError, ValidationError
from shiftcodes.repositories.database import UnitOfWork, create_session_factory
from shiftcodes.services.audit import AuditService
from tests.container_harness import DEV, Stack, build_stack
from tests.container_support import make_record, new_id

ACTOR = Actor(ActorType.USER, "1")
OTHER = Actor(ActorType.USER, "2")


@pytest.fixture
def stack(tmp_path: Path, engine: Engine, project: str) -> Stack:
    return build_stack(tmp_path, engine, project)


def _audit(engine: Engine, action: str) -> list[dict[str, object]]:
    service = AuditService(UnitOfWork(create_session_factory(engine)))
    return [dict(event.details) for event in service.list(action=action)]


def _web(stack: Stack) -> str:
    """Id of the current ``web`` container (recreating gives it a new id)."""
    infos = stack.services.inventory.list(stack.slug, DEV)
    return next(info.id for info in infos if info.service == "web")


# --- inventory and identity -------------------------------------------------------


def test_inventory_lists_only_the_projects_containers(stack: Stack) -> None:
    infos = stack.services.inventory.list(stack.slug, DEV)
    assert [i.service for i in infos] == ["web", "worker"]
    assert stack.foreign_id not in {i.id for i in infos}
    web = infos[0]
    assert (web.image, web.state, web.restart_count) == ("demo/web:1", "running", 0)
    assert web.ports[0].host_port == "8080"
    assert web.mounts[0].destination == "/data"
    assert web.started_at is not None


def test_inventory_never_contains_environment_values(stack: Stack) -> None:
    text = repr(stack.services.inventory.list(stack.slug, DEV))
    assert "hunter2-secret" not in text


def test_foreign_container_looks_missing(stack: Stack) -> None:
    with pytest.raises(NotFoundError):
        stack.services.inventory.get(stack.slug, DEV, stack.foreign_id)


def test_unknown_and_short_ids(stack: Stack) -> None:
    with pytest.raises(NotFoundError):
        stack.services.inventory.get(stack.slug, DEV, new_id())
    with pytest.raises(ValidationError):
        stack.services.inventory.get(stack.slug, DEV, stack.web_id[:12])


def test_environment_without_compose_project_cannot_be_managed(stack: Stack, project: str) -> None:
    with pytest.raises(ValidationError):
        stack.services.inventory.list(project, EnvironmentName.PRODUCTION)


# --- lifecycle --------------------------------------------------------------------


def test_plan_changes_nothing_and_confirm_executes(stack: Stack, engine: Engine) -> None:
    plan = stack.services.lifecycle.plan(
        stack.slug, DEV, stack.web_id, LifecycleAction.RESTART, ACTOR
    )
    assert plan.container.id == stack.web_id and plan.effects
    assert not any(call[0] == "restart" for call in stack.engine.calls)
    result = stack.services.lifecycle.confirm(
        stack.slug, DEV, stack.web_id, plan.confirmation, ACTOR
    )
    assert ("restart", stack.web_id) in stack.engine.calls
    assert result.container.restart_count == 1
    audit = _audit(engine, "container.restart")
    assert audit[0]["outcome"] == "done" and audit[0]["service"] == "web"


def test_confirmation_is_single_use_and_bound_to_actor_container_and_action(stack: Stack) -> None:
    lifecycle = stack.services.lifecycle
    plan = lifecycle.plan(stack.slug, DEV, stack.web_id, LifecycleAction.STOP, ACTOR)
    with pytest.raises(ValidationError):
        lifecycle.confirm(stack.slug, DEV, stack.web_id, plan.confirmation, OTHER)
    plan = lifecycle.plan(stack.slug, DEV, stack.web_id, LifecycleAction.STOP, ACTOR)
    with pytest.raises(ValidationError):
        lifecycle.confirm(stack.slug, DEV, stack.other_id, plan.confirmation, ACTOR)
    plan = lifecycle.plan(stack.slug, DEV, stack.web_id, LifecycleAction.STOP, ACTOR)
    lifecycle.confirm(stack.slug, DEV, stack.web_id, plan.confirmation, ACTOR)
    with pytest.raises(ValidationError):
        lifecycle.confirm(stack.slug, DEV, stack.web_id, plan.confirmation, ACTOR)
    assert [c for c in stack.engine.calls if c[0] == "stop"] == [("stop", stack.web_id)]


def test_unknown_confirmation_is_refused(stack: Stack) -> None:
    with pytest.raises(ValidationError):
        stack.services.lifecycle.confirm(stack.slug, DEV, stack.web_id, "nope", ACTOR)


def test_start_and_stop_preconditions(stack: Stack) -> None:
    with pytest.raises(ConflictError):
        stack.services.lifecycle.plan(stack.slug, DEV, stack.web_id, LifecycleAction.START, ACTOR)
    with pytest.raises(ConflictError):
        stack.services.lifecycle.plan(stack.slug, DEV, stack.other_id, LifecycleAction.STOP, ACTOR)
    plan = stack.services.lifecycle.plan(
        stack.slug, DEV, stack.other_id, LifecycleAction.START, ACTOR
    )
    result = stack.services.lifecycle.confirm(
        stack.slug, DEV, stack.other_id, plan.confirmation, ACTOR
    )
    assert result.container.state == "running" and result.previous_state == "exited"


def test_confirm_is_pinned_to_the_exact_container_id(stack: Stack) -> None:
    """If the container was replaced between plan and confirm nothing is touched."""
    plan = stack.services.lifecycle.plan(
        stack.slug, DEV, stack.web_id, LifecycleAction.RESTART, ACTOR
    )
    stack.engine.records.pop(stack.web_id)
    replacement = stack.engine.add(make_record(service="web", name="demo-dev-web-1"))
    with pytest.raises(NotFoundError):
        stack.services.lifecycle.confirm(stack.slug, DEV, stack.web_id, plan.confirmation, ACTOR)
    assert not any(call[0] == "restart" for call in stack.engine.calls)
    assert replacement in stack.engine.records


def test_docker_failure_is_audited_and_reported(stack: Stack, engine: Engine) -> None:
    stack.engine.fail["restart"] = "boom"
    plan = stack.services.lifecycle.plan(
        stack.slug, DEV, stack.web_id, LifecycleAction.RESTART, ACTOR
    )
    with pytest.raises(ConflictError, match="boom"):
        stack.services.lifecycle.confirm(stack.slug, DEV, stack.web_id, plan.confirmation, ACTOR)
    assert _audit(engine, "container.restart")[0]["outcome"] == "failed"


def test_container_operations_wait_for_a_running_deployment(stack: Stack) -> None:
    plan = stack.services.lifecycle.plan(
        stack.slug, DEV, stack.web_id, LifecycleAction.RESTART, ACTOR
    )
    with (
        stack.locks.hold(f"{stack.slug}/development", timeout=None),
        pytest.raises(EnvironmentBusyError),
    ):
        stack.services.lifecycle.confirm(stack.slug, DEV, stack.web_id, plan.confirmation, ACTOR)
    assert not any(call[0] == "restart" for call in stack.engine.calls)


# --- environment editor -----------------------------------------------------------


def test_env_list_is_masked_and_reveal_is_audited(stack: Stack, engine: Engine) -> None:
    items = stack.services.env.list(stack.slug, DEV, stack.web_id)
    by_name = {item.name: item for item in items}
    assert by_name["DB_PASSWORD"].sensitive and not by_name["PATH"].sensitive
    assert "hunter2-secret" not in repr(items)
    value = stack.services.env.reveal(stack.slug, DEV, stack.web_id, "DB_PASSWORD", ACTOR)
    assert value == "hunter2-secret"
    audit = _audit(engine, "container.env.reveal")
    assert audit == [
        {"project": "demo", "environment": "development", "service": "web", "name": "DB_PASSWORD"}
    ]
    assert "hunter2-secret" not in json.dumps(audit)
    with pytest.raises(NotFoundError):
        stack.services.env.reveal(stack.slug, DEV, stack.web_id, "NOPE", ACTOR)


def test_env_update_writes_override_and_recreates_only_that_service(
    stack: Stack, engine: Engine
) -> None:
    result = stack.services.env.update(
        stack.slug, DEV, stack.web_id, EnvChange(set={"MODE": "fast", "PRICE": "$5"}), ACTOR
    )
    assert result.restarted and result.containers[0].state == "running"
    assert [call[0] for call in stack.compose.calls] == ["web"]
    new_id_ = _web(stack)
    assert new_id_ != stack.web_id
    env = stack.services.env.list(stack.slug, DEV, new_id_)
    assert {i.name for i in env if i.overridden} == {"MODE", "PRICE"}
    assert stack.services.env.reveal(stack.slug, DEV, new_id_, "PRICE", ACTOR) == "$5"
    path = stack.store.path(EnvironmentKey(stack.slug, DEV))
    assert path.stat().st_mode & 0o777 == 0o600
    assert "$$5" in path.read_text()
    audit = _audit(engine, "container.env.update")[0]
    assert audit["outcome"] == "applied" and audit["names_set"] == ["MODE", "PRICE"]
    assert "fast" not in json.dumps(audit)


def test_env_update_removal_reverts_an_override(stack: Stack) -> None:
    stack.services.env.update(stack.slug, DEV, stack.web_id, EnvChange(set={"A": "1"}), ACTOR)
    stack.services.env.update(stack.slug, DEV, _web(stack), EnvChange(remove=("A",)), ACTOR)
    key = EnvironmentKey(stack.slug, DEV)
    assert stack.store.snapshot(key) is None  # nothing left to override


def test_env_update_failure_restores_previous_file_and_state(stack: Stack, engine: Engine) -> None:
    stack.services.env.update(stack.slug, DEV, stack.web_id, EnvChange(set={"MODE": "ok"}), ACTOR)
    key = EnvironmentKey(stack.slug, DEV)
    before = stack.store.snapshot(key)
    stack.compose.crash_when = ("MODE", "bad")
    with pytest.raises(ConflictError, match="previous configuration was restored"):
        stack.services.env.update(
            stack.slug, DEV, _web(stack), EnvChange(set={"MODE": "bad"}), ACTOR
        )
    assert stack.store.snapshot(key) == before
    current = stack.services.inventory.get(stack.slug, DEV, _web(stack))
    assert current.state == "running"
    assert stack.services.env.reveal(stack.slug, DEV, current.id, "MODE", ACTOR) == "ok"
    assert next(e["outcome"] for e in _audit(engine, "container.env.update")) == "restored"


def test_env_update_failure_without_previous_file_removes_the_new_one(stack: Stack) -> None:
    stack.compose.fail_on_call = {1}
    with pytest.raises(ConflictError, match="restored"):
        stack.services.env.update(stack.slug, DEV, stack.web_id, EnvChange(set={"A": "1"}), ACTOR)
    key = EnvironmentKey(stack.slug, DEV)
    assert stack.store.snapshot(key) is None
    assert len(stack.compose.calls) == 2  # the failed attempt and the rollback recreate


def test_failure_message_never_contains_the_new_value(stack: Stack) -> None:
    stack.compose.crash_when = ("TOKEN_X", "super-secret-value")
    with pytest.raises(ConflictError) as raised:
        stack.services.env.update(
            stack.slug, DEV, stack.web_id, EnvChange(set={"TOKEN_X": "super-secret-value"}), ACTOR
        )
    assert "super-secret-value" not in str(raised.value)


def test_stopped_service_stays_stopped(stack: Stack) -> None:
    result = stack.services.env.update(
        stack.slug, DEV, stack.other_id, EnvChange(set={"A": "1"}), ACTOR
    )
    assert not result.restarted
    assert result.containers[0].state == "exited"
    assert stack.compose.calls == [("worker", False, stack.compose.calls[0][2])]


def test_env_update_validation(stack: Stack) -> None:
    with pytest.raises(ValidationError):
        stack.services.env.update(
            stack.slug, DEV, stack.web_id, EnvChange(set={"1BAD": "x"}), ACTOR
        )
    with pytest.raises(ValidationError):
        stack.services.env.update(stack.slug, DEV, stack.web_id, EnvChange(), ACTOR)
    with pytest.raises(ValidationError):
        stack.services.env.update(
            stack.slug, DEV, stack.web_id, EnvChange(set={"A": "a\0b"}), ACTOR
        )
    assert stack.compose.calls == []


def test_env_update_waits_for_environment_lock(stack: Stack) -> None:
    with (
        stack.locks.hold(f"{stack.slug}/development", timeout=None),
        pytest.raises(EnvironmentBusyError),
    ):
        stack.services.env.update(stack.slug, DEV, stack.web_id, EnvChange(set={"A": "1"}), ACTOR)
    assert stack.compose.calls == []


# --- limits -----------------------------------------------------------------------


def test_limits_are_applied_persisted_and_audited(stack: Stack, engine: Engine) -> None:
    info = stack.services.limits.update(
        stack.slug, DEV, stack.web_id, LimitChange(cpus=1.5, memory="512m"), ACTOR
    )
    assert info.cpus == 1.5 and info.memory_bytes == 512 * 1024**2
    document = json.loads(stack.store.path(EnvironmentKey(stack.slug, DEV)).read_text())
    assert document["services"]["web"] == {"cpus": 1.5, "mem_limit": 512 * 1024**2}
    assert _audit(engine, "container.limits.update")[0]["outcome"] == "applied"


def test_limits_beyond_the_maxima_are_rejected(stack: Stack) -> None:
    for change in (
        LimitChange(cpus=64),
        LimitChange(memory="64g"),
        LimitChange(memory="1m"),
        LimitChange(),
    ):
        with pytest.raises(ValidationError):
            stack.services.limits.update(stack.slug, DEV, stack.web_id, change, ACTOR)
    assert not any(call[0] == "update_limits" for call in stack.engine.calls)


def test_limits_failure_restores_the_override_file(stack: Stack) -> None:
    stack.engine.fail["update_limits"] = "kernel says no"
    with pytest.raises(ConflictError):
        stack.services.limits.update(stack.slug, DEV, stack.web_id, LimitChange(cpus=1), ACTOR)
    key = EnvironmentKey(stack.slug, DEV)
    assert stack.store.snapshot(key) is None


def test_limits_keep_existing_env_overrides(stack: Stack) -> None:
    stack.services.env.update(stack.slug, DEV, stack.web_id, EnvChange(set={"A": "1"}), ACTOR)
    stack.services.limits.update(stack.slug, DEV, _web(stack), LimitChange(cpus=2), ACTOR)
    key = EnvironmentKey(stack.slug, DEV)
    service = stack.store.load(key).for_service("web")
    assert service.env == {"A": "1"} and service.cpus == 2.0


# --- files ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "relative/path",
        "/etc/../shadow",
        "/app/./x",
        "/app//x",
        "/app/x\n",
        "/proc/1/environ",
        "/sys/kernel",
        "/dev/mem",
        "/app/.env",
        "/app/.env.production",
        "/root/.ssh",
        "/home/u/.aws",
        "/x/server.pem",
        "/x/id_rsa",
        "/" + "a" * 1100,
        "/app/\0x",
    ],
)
def test_file_browser_rejects_traversal_and_protected_paths(stack: Stack, bad: str) -> None:
    with pytest.raises(ValidationError):
        stack.services.files.list(stack.slug, DEV, stack.web_id, bad)
    with pytest.raises(ValidationError):
        stack.services.files.read(stack.slug, DEV, stack.web_id, bad, ACTOR)
    assert not any(call[0] in {"list_directory", "read_file"} for call in stack.engine.calls)


def test_directory_listing_sorts_and_hides_protected_names(stack: Stack) -> None:
    stack.engine.directories["/app"] = ["b.txt", "src/", ".env", "id_rsa", "a.txt", "bad\tname"]
    listing = stack.services.files.list(stack.slug, DEV, stack.web_id, "/app")
    assert [(e.name, e.kind) for e in listing.entries] == [
        ("src", "directory"),
        ("a.txt", "file"),
        ("b.txt", "file"),
    ]


def test_listing_needs_a_running_container_and_a_real_path(stack: Stack) -> None:
    with pytest.raises(ConflictError):
        stack.services.files.list(stack.slug, DEV, stack.other_id, "/app")
    with pytest.raises(NotFoundError):
        stack.services.files.list(stack.slug, DEV, stack.web_id, "/missing")


def test_read_text_binary_truncated_and_non_files(stack: Stack, engine: Engine) -> None:
    files = stack.engine.files
    files["/app/a.txt"] = FileRead("file", 5, b"hello", False)
    files["/app/big.txt"] = FileRead("file", 10, b"0123456789", False)
    files["/app/bin"] = FileRead("file", 4, b"\x00\x01\x02\x03", False)
    files["/app/latin"] = FileRead("file", 3, b"\xe9\xe9\xe9", False)
    files["/app/dir"] = FileRead("directory", 0, b"", False)
    files["/app/link"] = FileRead("symlink", 0, b"", False)
    read = stack.services.files.read
    assert read(stack.slug, DEV, stack.web_id, "/app/a.txt", ACTOR).text == "hello"
    big = read(stack.slug, DEV, stack.web_id, "/app/big.txt", ACTOR, max_bytes=4)
    assert (big.text, big.truncated, big.size) == ("0123", True, 10)
    assert read(stack.slug, DEV, stack.web_id, "/app/bin", ACTOR).binary
    assert read(stack.slug, DEV, stack.web_id, "/app/latin", ACTOR).binary
    for name in ("dir", "link"):
        with pytest.raises(ValidationError):
            read(stack.slug, DEV, stack.web_id, f"/app/{name}", ACTOR)
    with pytest.raises(NotFoundError):
        read(stack.slug, DEV, stack.web_id, "/app/missing", ACTOR)
    paths = [event["path"] for event in _audit(engine, "container.file.read")]
    assert paths.count("/app/a.txt") == 1 and "/app/missing" not in paths


def test_multibyte_character_cut_by_the_cap_is_not_binary(stack: Stack) -> None:
    data = "aé".encode()  # 3 bytes
    stack.engine.files["/app/u.txt"] = FileRead("file", 3, data, False)
    result = stack.services.files.read(
        stack.slug, DEV, stack.web_id, "/app/u.txt", ACTOR, max_bytes=2
    )
    assert not result.binary and result.text == "a" and result.truncated


# --- logs -------------------------------------------------------------------------

T = "2026-10-13T12:00:0{n}.000000000Z"


def _lines(stack: Stack, container_id: str, texts: list[str]) -> None:
    stack.engine.log_lines[container_id] = [f"{T.format(n=i)} {t}" for i, t in enumerate(texts)]


def test_logs_are_masked_and_bounded(stack: Stack) -> None:
    _lines(
        stack,
        stack.web_id,
        [
            "connecting with hunter2-secret now",
            "password=abc123 token: xyz",
            "x" * 20000,
        ],
    )
    page = stack.services.logs.read(stack.slug, DEV, stack.web_id, tail=10)
    texts = [line.text for line in page.lines]
    assert "hunter2-secret" not in texts[0] and "[REDACTED]" in texts[0]
    assert "abc123" not in texts[1] and "xyz" not in texts[1]
    assert len(texts[2]) < 9000 and texts[2].endswith("[line truncated]")


def test_log_tail_is_clamped(stack: Stack) -> None:
    _lines(stack, stack.web_id, ["a"])
    stack.services.logs.read(stack.slug, DEV, stack.web_id, tail=10**6)
    assert ("logs", stack.web_id, "1000", "None") in stack.engine.calls


def test_log_follow_delivers_each_line_exactly_once(stack: Stack) -> None:
    logs = stack.services.logs
    _lines(stack, stack.web_id, ["one", "two"])
    first = logs.read(stack.slug, DEV, stack.web_id, tail=10)
    assert [line.text for line in first.lines] == ["one", "two"]
    assert first.cursor is not None
    again = logs.read(stack.slug, DEV, stack.web_id, cursor=first.cursor)
    assert again.lines == ()
    # a new line at the same timestamp as the last one and one later
    stack.engine.log_lines[stack.web_id] += [f"{T.format(n=1)} two-b", f"{T.format(n=3)} three"]
    stack.engine.log_lines[stack.web_id].sort(key=lambda line: line.split(" ", 1)[0])
    more = logs.read(stack.slug, DEV, stack.web_id, cursor=again.cursor)
    assert [line.text for line in more.lines] == ["two-b", "three"]


def test_log_follow_caps_a_burst_and_reports_dropped(stack: Stack) -> None:
    stack.engine.log_lines[stack.web_id] = [
        f"2026-10-13T12:00:00.{i:09d}Z line{i}" for i in range(900)
    ]
    cursor = LogCursor("2026-10-13T11:59:00.000000000Z", 0)
    page = stack.services.logs.read(stack.slug, DEV, stack.web_id, cursor=cursor)
    assert len(page.lines) == 500 and page.dropped == 400
    assert page.lines[-1].text == "line899"


def test_empty_log_still_yields_a_cursor_for_following(stack: Stack) -> None:
    page = stack.services.logs.read(stack.slug, DEV, stack.web_id, tail=5)
    assert page.lines == () and page.cursor is not None


def test_lock_registry_is_shared_by_key() -> None:
    locks = EnvironmentLocks()
    entered = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with locks.hold("p/e", timeout=None):
            entered.set()
            release.wait(2)

    thread = threading.Thread(target=holder)
    thread.start()
    entered.wait(2)
    assert locks.is_held("p/e") and not locks.is_held("p/other")
    with pytest.raises(EnvironmentBusyError), locks.hold("p/e", timeout=0.3):
        pass
    release.set()
    thread.join()
    assert not locks.is_held("p/e")


def test_deployments_hold_the_environment_lock_and_honour_cancellation() -> None:
    locks = EnvironmentLocks()
    seen: list[bool] = []

    class Inner:
        def deploy(self, request: DeployRequest, sink: LineSink, cancel: CancelSignal) -> None:
            seen.append(locks.is_held("demo/development"))

    request = DeployRequest("demo", "development", "demo-dev", (), (), 1, "a" * 40)
    LockedDeployer(Inner(), locks).deploy(request, print, threading.Event())
    assert seen == [True] and not locks.is_held("demo/development")
    cancelled = threading.Event()
    cancelled.set()
    with locks.hold("demo/development", timeout=None), pytest.raises(EnvironmentBusyError):
        LockedDeployer(Inner(), locks).deploy(request, print, cancelled)
    assert seen == [True]
