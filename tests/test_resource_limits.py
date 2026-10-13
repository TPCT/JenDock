"""Resource limits per compose service: validation, override rendering, the service on fakes."""

from __future__ import annotations

import json
import shutil
import stat
import subprocess
import threading
from pathlib import Path

import pytest
from sqlalchemy import Engine

from backend.adapters.compose_overrides import OverrideFileError
from backend.adapters.container_admin import LiveUpdate
from backend.adapters.deployer import DeployRequest
from backend.adapters.docker_stats import ContainerStats
from backend.adapters.resource_overrides import FILE_NAME, ResourceOverrideStore, render_limits
from backend.containers.locks import EnvironmentLocks, LockedDeployer, managed_files
from backend.containers.resources import ResourceOverrideFiles, live_update
from backend.domain.actor import Actor
from backend.domain.containers import EnvChange, EnvironmentKey, LimitChange
from backend.domain.enums import ActorType
from backend.domain.errors import ConflictError, NotFoundError, ValidationError
from backend.domain.resources import (
    GIB,
    MIB,
    HostCapacity,
    ResourceLimits,
    format_size,
    validate_limits,
    validate_service_name,
)
from backend.repositories.database import UnitOfWork, create_session_factory
from backend.services.audit import AuditService
from backend.services.environments import EnvironmentService, EnvironmentUpdate
from tests.container_harness import DEV, Stack, build_stack

ACTOR = Actor(ActorType.USER, "1")
HOST = HostCapacity(cpus=4, memory_bytes=8 * GIB)


@pytest.fixture
def stack(tmp_path: Path, engine: Engine, project: str) -> Stack:
    return build_stack(tmp_path, engine, project)


def _audit(engine: Engine, action: str) -> list[dict[str, object]]:
    service = AuditService(UnitOfWork(create_session_factory(engine)))
    return [dict(event.details) for event in service.list(action=action)]


def _host_config(stack: Stack, container_id: str) -> dict[str, object]:
    host = stack.engine.records[container_id]["HostConfig"]
    assert isinstance(host, dict)
    return host


def _save(
    stack: Stack, limits: ResourceLimits, *, service: str = "web", apply_now: bool = False
) -> object:
    return stack.services.resources.save(
        stack.slug, DEV, service, limits, ACTOR, apply_now=apply_now
    )


# --- validation -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("limits", "message"),
    [
        (ResourceLimits(), "at least one limit"),
        (ResourceLimits(cpus=0), "greater than 0"),
        (ResourceLimits(cpus=-1), "greater than 0"),
        (ResourceLimits(cpus=0.001), "at least 0.01"),
        (ResourceLimits(cpus=4.5), "cannot exceed the host's 4 CPUs"),
        (ResourceLimits(memory_bytes=5 * MIB), "memory must be at least 6 MiB"),
        (ResourceLimits(memory_bytes=9 * GIB), "cannot exceed the host's 8 GiB"),
        (ResourceLimits(memory_reservation_bytes=MIB), "memory reservation must be at least"),
        (
            ResourceLimits(memory_bytes=512 * MIB, memory_reservation_bytes=GIB),
            "reservation cannot be larger",
        ),
        (ResourceLimits(cpus=1, cpu_shares=1), "cpu shares must be between"),
        (ResourceLimits(cpus=1, cpu_shares=300_000), "cpu shares must be between"),
    ],
)
def test_invalid_limits_say_why(limits: ResourceLimits, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        validate_limits(limits, HOST)


@pytest.mark.parametrize(
    "limits",
    [
        ResourceLimits(cpus=0.01),
        ResourceLimits(cpus=4),
        ResourceLimits(memory_bytes=6 * MIB),
        ResourceLimits(memory_bytes=8 * GIB),
        ResourceLimits(cpus=2, memory_bytes=GIB, memory_reservation_bytes=GIB, cpu_shares=512),
    ],
)
def test_limits_on_the_edge_are_valid(limits: ResourceLimits) -> None:
    assert validate_limits(limits, HOST) == limits


def test_swap_equals_memory_so_the_limit_is_real() -> None:
    assert ResourceLimits(memory_bytes=GIB).memory_swap_bytes == GIB
    assert ResourceLimits(cpus=1).memory_swap_bytes is None


def test_service_names_and_sizes() -> None:
    assert validate_service_name("web-1.api_x") == "web-1.api_x"
    for bad in ("", "-web", "we b", "a/b", "x" * 65, "web\n"):
        with pytest.raises(ValidationError):
            validate_service_name(bad)
    assert format_size(512 * MIB) == "512 MiB" and format_size(3 * GIB // 2) == "1.5 GiB"


# --- override rendering -----------------------------------------------------------


def test_render_uses_service_level_keys_and_resets_the_deploy_block() -> None:
    text = render_limits(
        {
            "web": ResourceLimits(
                cpus=0.5, memory_bytes=512 * MIB, memory_reservation_bytes=MIB * 64, cpu_shares=512
            ),
            "worker": ResourceLimits(cpu_shares=256),
            "idle": ResourceLimits(),
        }
    )
    assert "x-jendock-resource-limits:" in text
    assert (
        '  "web":\n    cpus: 0.5\n    mem_limit: 536870912\n    memswap_limit: 536870912\n' in text
    )
    assert "    mem_reservation: 67108864\n    cpu_shares: 512\n" in text
    assert "limits: !reset null" in text
    worker = text.split('"worker":')[1]
    assert "cpu_shares: 256" in worker and "!reset" not in worker  # nothing to conflict with
    assert "idle" not in text


def test_render_without_limits_is_an_empty_services_map() -> None:
    assert render_limits({}).endswith("services: {}\n")


def test_store_writes_atomically_and_removes_the_file(tmp_path: Path) -> None:
    store = ResourceOverrideStore(tmp_path)
    key = EnvironmentKey("demo", DEV)
    assert store.files(key) == []
    path = store.write(key, {"web": ResourceLimits(cpus=1)})
    assert path == tmp_path / "demo" / "development" / FILE_NAME
    assert store.files(key) == [str(path)]
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert store.write(key, {"web": ResourceLimits()}) is None
    assert store.files(key) == [] and not path.exists()


@pytest.mark.skipif(shutil.which("docker") is None, reason="needs the docker CLI (no daemon)")
def test_compose_accepts_the_override_over_both_limit_styles(tmp_path: Path) -> None:
    """``docker compose config`` (no daemon) merges the file with a repository that declares its
    limits in either style; the Jendock values win and Compose reports no conflict."""
    base = tmp_path / "compose.yml"
    base.write_text(
        "services:\n"
        "  web:\n    image: alpine\n    deploy:\n      resources:\n        limits:\n"
        '          cpus: "1"\n          memory: 256M\n'
        "  api:\n    image: alpine\n    mem_limit: 128m\n    cpus: 1\n"
        "  plain:\n    image: alpine\n"
    )
    store = ResourceOverrideStore(tmp_path / "ov")
    path = store.write(
        EnvironmentKey("demo", DEV),
        {
            "web": ResourceLimits(cpus=0.5, memory_bytes=512 * MIB, cpu_shares=512),
            "api": ResourceLimits(cpus=2, memory_bytes=GIB),
            "plain": ResourceLimits(memory_bytes=64 * MIB, memory_reservation_bytes=32 * MIB),
        },
    )
    assert path is not None
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [
            str(shutil.which("docker")),
            "compose",
            "-f",
            str(base),
            "-f",
            str(path),
            "config",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    services = json.loads(result.stdout)["services"]
    assert services["web"]["cpus"] == 0.5 and services["web"]["mem_limit"] == str(512 * MIB)
    assert services["web"]["memswap_limit"] == str(512 * MIB)
    assert services["web"]["cpu_shares"] == 512
    assert "limits" not in services["web"].get("deploy", {}).get("resources", {})
    assert services["api"]["cpus"] == 2 and services["api"]["mem_limit"] == str(GIB)
    assert services["plain"]["mem_reservation"] == str(32 * MIB)


# --- live update values -----------------------------------------------------------


def test_live_update_sends_memory_with_its_swap_limit() -> None:
    update, notes = live_update(
        ResourceLimits(cpus=1.5, memory_bytes=512 * MIB, cpu_shares=256), None, HOST
    )
    assert update == LiveUpdate(
        cpus=1.5, memory_bytes=512 * MIB, memory_swap_bytes=512 * MIB, cpu_shares=256
    )
    assert notes == ()


def test_live_update_lifts_removed_limits_to_the_host() -> None:
    before = ResourceLimits(
        cpus=1, memory_bytes=GIB, memory_reservation_bytes=MIB * 64, cpu_shares=512
    )
    update, notes = live_update(ResourceLimits(), before, HOST)
    assert update == LiveUpdate(
        cpus=4.0, memory_bytes=8 * GIB, memory_swap_bytes=-1, cpu_shares=1024
    )
    assert len(notes) == 2 and "raised to the host" in notes[0] and "reservation" in notes[1]


def test_live_update_leaves_what_was_never_limited() -> None:
    update, notes = live_update(ResourceLimits(cpus=2), ResourceLimits(cpus=1), HOST)
    assert update == LiveUpdate(cpus=2.0) and notes == ()
    assert live_update(ResourceLimits(), None, HOST)[0].is_empty


# --- the service on fake Docker ---------------------------------------------------


def test_save_persists_and_renders_the_override(stack: Stack, engine: Engine) -> None:
    limits = ResourceLimits(cpus=1.5, memory_bytes=512 * MIB, cpu_shares=512)
    change = stack.services.resources.save(stack.slug, DEV, "web", limits, ACTOR)
    assert change.limits == limits and change.saved and change.applied == ()
    overview = stack.services.resources.overview(stack.slug, DEV)
    assert overview.host == HostCapacity(4, 8 * GIB)
    web = next(item for item in overview.services if item.service == "web")
    assert web.saved and web.limits == limits and web.updated_at is not None
    worker = next(item for item in overview.services if item.service == "worker")
    assert not worker.saved and worker.limits.is_empty
    path = stack.limit_files.path(EnvironmentKey(stack.slug, DEV))
    assert "cpus: 1.5" in path.read_text() and "mem_limit: 536870912" in path.read_text()
    details = _audit(engine, "container.resources.update")[0]
    assert details["service"] == "web" and details["memory_bytes"] == 512 * MIB
    assert not any(call[0] == "update_resources" for call in stack.engine.calls)  # not applied


def test_save_replaces_the_whole_set_of_limits(stack: Stack) -> None:
    _save(stack, ResourceLimits(cpus=1, memory_bytes=GIB))
    _save(stack, ResourceLimits(cpu_shares=300))
    saved = stack.limit_store.get(stack.environment_id, "web")
    assert saved is not None and saved.limits == ResourceLimits(cpu_shares=300)
    assert "mem_limit" not in stack.limit_files.path(EnvironmentKey(stack.slug, DEV)).read_text()


def test_apply_now_updates_only_the_running_containers_of_the_service(stack: Stack) -> None:
    change = stack.services.resources.save(
        stack.slug,
        DEV,
        "web",
        ResourceLimits(cpus=2, memory_bytes=256 * MIB, memory_reservation_bytes=64 * MIB),
        ACTOR,
        apply_now=True,
    )
    assert [(item.id, item.ok) for item in change.applied] == [(stack.web_id, True)]
    host = _host_config(stack, stack.web_id)
    assert host["NanoCpus"] == 2_000_000_000
    assert host["Memory"] == host["MemorySwap"] == 256 * MIB
    assert host["MemoryReservation"] == 64 * MIB
    updated = [call for call in stack.engine.calls if call[0] == "update_resources"]
    assert [call[1] for call in updated] == [stack.web_id]  # not the stopped worker, not foreign
    assert _host_config(stack, stack.foreign_id)["Memory"] == 0


def test_lowering_memory_below_the_current_swap_does_not_fail(stack: Stack) -> None:
    _save(stack, ResourceLimits(memory_bytes=GIB), apply_now=True)
    change = stack.services.resources.save(
        stack.slug, DEV, "web", ResourceLimits(memory_bytes=128 * MIB), ACTOR, apply_now=True
    )
    assert change.applied[0].ok
    assert _host_config(stack, stack.web_id)["MemorySwap"] == 128 * MIB
    # and raising it again works because memory and swap travel together
    again = stack.services.resources.save(
        stack.slug, DEV, "web", ResourceLimits(memory_bytes=2 * GIB), ACTOR, apply_now=True
    )
    assert again.applied[0].ok


def test_apply_reports_a_container_that_docker_refuses(stack: Stack) -> None:
    stack.engine.fail["update_resources"] = "kernel says no"
    change = stack.services.resources.save(
        stack.slug, DEV, "web", ResourceLimits(cpus=1), ACTOR, apply_now=True
    )
    assert change.saved and not change.applied[0].ok
    assert change.applied[0].error == "kernel says no"
    saved = stack.limit_store.get(stack.environment_id, "web")
    assert saved is not None  # the limits are saved for future deploys anyway


def test_apply_without_running_containers_says_so(stack: Stack) -> None:
    change = stack.services.resources.save(
        stack.slug, DEV, "worker", ResourceLimits(cpus=1), ACTOR, apply_now=True
    )
    assert change.applied == () and "No container of this service is running" in change.notes[0]


def test_apply_copies_the_saved_limits(stack: Stack, engine: Engine) -> None:
    _save(stack, ResourceLimits(cpus=1, memory_bytes=300 * MIB))
    assert _host_config(stack, stack.web_id)["Memory"] == 0
    change = stack.services.resources.apply(stack.slug, DEV, "web", ACTOR)
    assert change.applied[0].ok and _host_config(stack, stack.web_id)["Memory"] == 300 * MIB
    assert _audit(engine, "container.resources.apply")[0]["applied_containers"] == 1
    with pytest.raises(NotFoundError):
        stack.services.resources.apply(stack.slug, DEV, "worker", ACTOR)


def test_remove_deletes_the_row_and_the_file_and_lifts_live(stack: Stack, engine: Engine) -> None:
    _save(stack, ResourceLimits(cpus=1, memory_bytes=GIB), apply_now=True)
    path = stack.limit_files.path(EnvironmentKey(stack.slug, DEV))
    assert path.exists()
    change = stack.services.resources.remove(stack.slug, DEV, "web", ACTOR, apply_now=True)
    assert not change.saved and change.limits.is_empty and change.applied[0].ok
    assert not path.exists()
    assert stack.limit_store.get(stack.environment_id, "web") is None
    host = _host_config(stack, stack.web_id)
    assert host["NanoCpus"] == 4_000_000_000 and host["MemorySwap"] == -1
    assert _audit(engine, "container.resources.remove")[0]["previous_cpus"] == 1
    with pytest.raises(NotFoundError):
        stack.services.resources.remove(stack.slug, DEV, "web", ACTOR)


def test_remove_without_apply_leaves_running_containers_alone(stack: Stack) -> None:
    _save(stack, ResourceLimits(cpus=1), apply_now=True)
    stack.engine.calls.clear()
    stack.services.resources.remove(stack.slug, DEV, "web", ACTOR)
    assert not any(call[0] == "update_resources" for call in stack.engine.calls)


def test_unknown_service_and_invalid_values_change_nothing(stack: Stack) -> None:
    with pytest.raises(NotFoundError):
        _save(stack, ResourceLimits(cpus=1), service="nope")
    with pytest.raises(ValidationError):
        _save(stack, ResourceLimits(cpus=99))
    with pytest.raises(ValidationError):
        _save(stack, ResourceLimits(), service="../x")
    assert stack.services.resources.overview(stack.slug, DEV).services[0].saved is False
    assert not stack.limit_files.path(EnvironmentKey(stack.slug, DEV)).exists()


def test_a_service_configured_for_deployment_may_be_limited_before_it_runs(
    stack: Stack, engine: Engine
) -> None:
    EnvironmentService(UnitOfWork(create_session_factory(engine))).update(
        stack.slug, DEV, EnvironmentUpdate(services=["web", "worker", "cron"])
    )
    change = stack.services.resources.save(
        stack.slug, DEV, "cron", ResourceLimits(memory_bytes=64 * MIB), ACTOR
    )
    assert change.saved
    names = [item.service for item in stack.services.resources.overview(stack.slug, DEV).services]
    assert names == ["cron", "web", "worker"]


def test_file_failure_rolls_back_and_nothing_is_saved(
    stack: Stack, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*_args: object, **_kwargs: object) -> None:
        raise OverrideFileError("disk full")

    monkeypatch.setattr(stack.limit_files, "write", boom)
    with pytest.raises(ConflictError, match="disk full"):
        _save(stack, ResourceLimits(cpus=1))
    assert stack.limit_store.get(stack.environment_id, "web") is None


def test_saved_limits_replace_the_older_per_container_ones(stack: Stack) -> None:
    stack.services.limits.update(stack.slug, DEV, stack.web_id, LimitChange(cpus=2), ACTOR)
    key = EnvironmentKey(stack.slug, DEV)
    assert stack.store.load(key).for_service("web").cpus == 2.0
    _save(stack, ResourceLimits(memory_bytes=GIB))
    assert stack.store.load(key).for_service("web").cpus is None


def test_a_recreate_keeps_the_limits(stack: Stack) -> None:
    _save(stack, ResourceLimits(cpus=1))
    stack.services.env.update(stack.slug, DEV, stack.web_id, EnvChange(set={"A": "1"}), ACTOR)
    files = stack.compose.calls[-1][2]
    assert any(Path(item).name == FILE_NAME for item in files)


def test_every_deploy_renders_the_limits_before_the_image_override(
    stack: Stack, tmp_path: Path
) -> None:
    _save(stack, ResourceLimits(memory_bytes=GIB))
    stack.limit_files.path(EnvironmentKey(stack.slug, DEV)).unlink()  # a stale or lost file
    seen: list[DeployRequest] = []

    class Inner:
        def deploy(self, request: DeployRequest, sink: object, cancel: object) -> None:
            seen.append(request)

    files = ResourceOverrideFiles(stack.limit_store, stack.limit_files)
    hook = managed_files(tmp_path / "overrides", files.render)
    request = DeployRequest(
        stack.slug, "development", "demo-dev", ("/r/compose.yml",), (), 1, "a" * 40
    )
    LockedDeployer(Inner(), EnvironmentLocks(), hook).deploy(request, print, threading.Event())
    compose_files = seen[0].compose_files
    assert compose_files[0] == "/r/compose.yml" and Path(compose_files[-1]).name == FILE_NAME
    assert "mem_limit: 1073741824" in Path(compose_files[-1]).read_text()


def test_usage_reports_running_containers_against_their_limits(stack: Stack) -> None:
    _save(stack, ResourceLimits(cpus=2, memory_bytes=512 * MIB), apply_now=True)
    stack.engine.sample_stats = [
        ContainerStats(stack.web_id, "demo-web-1", 150.5, 100 * MIB, 512 * MIB, 19.5, 1, 2, 3, 4, 7)
    ]
    usage = stack.services.resources.usage(stack.slug, DEV)
    assert usage.host == HostCapacity(4, 8 * GIB)
    [web] = usage.containers
    assert (web.service, web.cpu_percent, web.memory_used) == ("web", 150.5, 100 * MIB)
    assert (web.cpu_limit, web.memory_limit) == (2.0, 512 * MIB)


def test_usage_without_running_containers_still_reports_the_host(stack: Stack) -> None:
    stack.engine.records[stack.web_id]["State"] = {"Status": "exited", "Running": False}
    assert stack.services.resources.usage(stack.slug, DEV).containers == ()


def test_docker_being_down_is_a_conflict_not_a_crash(stack: Stack) -> None:
    stack.engine.fail["host_capacity"] = "docker is not usable"
    with pytest.raises(ConflictError, match="not usable"):
        stack.services.resources.overview(stack.slug, DEV)
