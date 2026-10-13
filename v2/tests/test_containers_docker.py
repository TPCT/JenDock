"""Container management and terminal against a real Docker daemon (``make test-docker``).

A throwaway compose project is created per test. Everything carries the labels
``shiftcodes.test=1`` and ``shiftcodes.test.owner=<unique id>``; teardown removes only what
carries this run's owner label (other test runs may be active on the same daemon) and verifies
that nothing of ours is left.
"""

from __future__ import annotations

import json
import secrets
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import Engine

from shiftcodes.adapters.docker_client import DockerClient
from shiftcodes.config import Settings
from shiftcodes.containers.locks import EnvironmentLocks
from shiftcodes.containers.logs import LogCursor
from shiftcodes.domain.actor import Actor
from shiftcodes.domain.containers import EnvChange, EnvironmentKey, LifecycleAction, LimitChange
from shiftcodes.domain.enums import ActorType, EnvironmentName
from shiftcodes.domain.errors import ConflictError, NotFoundError, ValidationError
from shiftcodes.repositories.database import UnitOfWork, create_session_factory
from shiftcodes.services.audit import AuditService
from shiftcodes.services.environments import EnvironmentService, EnvironmentUpdate
from shiftcodes.worker.container_wiring import ContainerWorkerParts, build_container_parts
from tests.api_support import make_settings
from tests.docker_support import docker_available, image_present
from tests.terminal_support import Client, wait_for

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not docker_available(), reason="no Docker daemon available"),
]
DEV = EnvironmentName.DEVELOPMENT
ACTOR = Actor(ActorType.USER, "1")
BASE_CANDIDATES = ("alpine:3.20", "alpine:3.24", "python:3.13-alpine")
SECRET_WORD = "s3cret-pass-word"


def docker(*args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    """Run the docker CLI (fixed argv in tests)."""
    return subprocess.run(  # noqa: S603
        ["docker", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,  # noqa: S607
    )


def base_image() -> str:
    """A local image with busybox tools; never pulls when one of the candidates exists."""
    for candidate in BASE_CANDIDATES:
        if image_present(candidate):
            return candidate
    pytest.skip("no local alpine-based image; not pulling images in this test")


@dataclass
class Live:
    """A real compose project wired to the real services."""

    owner: str
    project: str
    slug: str
    compose_file: Path
    parts: ContainerWorkerParts
    settings: Settings
    engine: Engine

    def containers(self, service: str | None = None) -> list[dict[str, object]]:
        """Inspect records of our project's containers."""
        ids = docker(
            "ps",
            "-aq",
            "--no-trunc",
            "--filter",
            f"label=com.docker.compose.project={self.project}",
        ).stdout.split()
        if not ids:
            return []
        records: list[dict[str, object]] = json.loads(docker("inspect", *ids).stdout)
        if service is None:
            return records
        return [
            r for r in records if r["Config"]["Labels"]["com.docker.compose.service"] == service
        ]  # type: ignore[index]

    def one(self, service: str) -> dict[str, object]:
        """The single container of ``service``."""
        [record] = self.containers(service)
        return record

    def cid(self, service: str) -> str:
        """Id of the container of ``service``."""
        return str(self.one(service)["Id"])

    def env_of(self, service: str) -> dict[str, str]:
        """The container's environment."""
        config = self.one(service)["Config"]
        assert isinstance(config, dict)
        return dict(item.split("=", 1) for item in config["Env"])

    def audit(self, action: str) -> list[dict[str, object]]:
        """Audit details of ``action``."""
        service = AuditService(UnitOfWork(create_session_factory(self.engine)))
        return [dict(event.details) for event in service.list(action=action)]

    def compose(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run ``docker compose`` for the project."""
        return docker("compose", "-p", self.project, "-f", str(self.compose_file), *args)


COMPOSE = """services:
  web:
    image: {image}
    command: ["sh", "-c", "echo starting-with-{secret}; test \\"$$MODE\\" != bad || exit 3; sleep 100000"]
    environment:
      MODE: ok
      DB_PASSWORD: {secret}
    ports: ["127.0.0.1::8080"]
    volumes: [data:/data]
    labels: {{shiftcodes.test: "1", shiftcodes.test.owner: "{owner}"}}
  idle:
    image: {image}
    command: ["sleep", "100000"]
    labels: {{shiftcodes.test: "1", shiftcodes.test.owner: "{owner}"}}
volumes:
  data:
    labels: {{shiftcodes.test: "1", shiftcodes.test.owner: "{owner}"}}
"""


def _leftovers(owner: str) -> list[str]:
    label = f"label=shiftcodes.test.owner={owner}"
    found = docker("ps", "-aq", "--filter", label).stdout.split()
    found += docker("volume", "ls", "-q", "--filter", label).stdout.split()
    return found


@pytest.fixture
def live(tmp_path: Path, socket_dir: Path, engine: Engine, project: str) -> Iterator[Live]:
    owner = f"task8-{secrets.token_hex(4)}"
    compose_project = f"sc{secrets.token_hex(4)}"
    compose_file = tmp_path / "repo" / "compose.yml"
    compose_file.parent.mkdir(parents=True)
    compose_file.write_text(COMPOSE.format(image=base_image(), secret=SECRET_WORD, owner=owner))
    factory = create_session_factory(engine)
    EnvironmentService(UnitOfWork(factory)).update(
        project,
        DEV,
        EnvironmentUpdate(compose_project=compose_project, compose_files=[str(compose_file)]),
    )
    settings = make_settings(tmp_path, socket_path=socket_dir / "w.sock").model_copy(
        update={
            "docker_host": "",
            "container_verify_timeout": 25.0,
            "terminal_idle_seconds": 1.0,
            "terminal_max_seconds": 60.0,
        }
    )
    parts = build_container_parts(settings, factory, EnvironmentLocks())
    live = Live(owner, compose_project, project, compose_file, parts, settings, engine)
    up = live.compose("up", "-d", "--wait")
    assert up.returncode == 0, up.stderr
    parts.terminals.start()
    parts.terminal_server.start()
    try:
        yield live
    finally:
        parts.terminal_server.stop()
        parts.terminals.shutdown()
        live.compose("down", "--volumes", "--remove-orphans", "--timeout", "1")
        for leftover in docker(
            "ps", "-aq", "--filter", f"label=shiftcodes.test.owner={owner}"
        ).stdout.split():
            docker("rm", "-f", leftover)
        docker("volume", "rm", "-f", f"{compose_project}_data")
        assert _leftovers(owner) == []
        networks = docker(
            "network", "ls", "-q", "--filter", f"label=com.docker.compose.project={compose_project}"
        ).stdout.split()
        assert networks == []


# --- inventory --------------------------------------------------------------------


def test_inventory_of_a_real_compose_project(live: Live) -> None:
    stranger = docker(
        "run", "-d", "--label", "shiftcodes.test=1", "--label", f"shiftcodes.test.owner={live.owner}",
        base_image(), "sleep", "100000",
    )  # fmt: skip
    assert stranger.returncode == 0
    try:
        infos = live.parts.services.inventory.list(live.slug, DEV)
        assert [(i.service, i.state) for i in infos] == [("idle", "running"), ("web", "running")]
        web = next(i for i in infos if i.service == "web")
        assert web.image == base_image() and web.image_id.startswith("sha256:")
        assert web.restart_count == 0 and web.started_at is not None
        assert any(p.container_port == "8080/tcp" and p.host_ip == "127.0.0.1" for p in web.ports)
        assert any(m.destination == "/data" and m.type == "volume" for m in web.mounts)
        assert stranger.stdout.strip() not in {i.id for i in infos}
        with pytest.raises(NotFoundError):
            live.parts.services.inventory.get(live.slug, DEV, stranger.stdout.strip())
        assert SECRET_WORD not in repr(infos)
    finally:
        docker("rm", "-f", stranger.stdout.strip())


# --- lifecycle --------------------------------------------------------------------


def test_restart_is_pinned_to_the_exact_container(live: Live) -> None:
    lifecycle = live.parts.services.lifecycle
    web = live.cid("web")
    before = live.one("web")["State"]["StartedAt"]  # type: ignore[index]
    plan = lifecycle.plan(live.slug, DEV, web, LifecycleAction.RESTART, ACTOR)
    result = lifecycle.confirm(live.slug, DEV, web, plan.confirmation, ACTOR)
    assert result.container.id == web and result.container.state == "running"
    assert live.one("web")["State"]["StartedAt"] != before  # type: ignore[index]
    assert live.audit("container.restart")[0]["outcome"] == "done"
    # planned against one container, then it is replaced: nothing may happen to the new one
    plan = lifecycle.plan(live.slug, DEV, web, LifecycleAction.RESTART, ACTOR)
    assert live.compose("up", "-d", "--force-recreate", "--no-deps", "web").returncode == 0
    replacement = live.cid("web")
    assert replacement != web
    started = live.one("web")["State"]["StartedAt"]  # type: ignore[index]
    with pytest.raises(NotFoundError):
        lifecycle.confirm(live.slug, DEV, web, plan.confirmation, ACTOR)
    assert live.one("web")["State"]["StartedAt"] == started  # type: ignore[index]
    assert live.one("web")["RestartCount"] == 0


def test_stop_and_start(live: Live) -> None:
    lifecycle = live.parts.services.lifecycle
    idle = live.cid("idle")
    plan = lifecycle.plan(live.slug, DEV, idle, LifecycleAction.STOP, ACTOR)
    assert (
        lifecycle.confirm(live.slug, DEV, idle, plan.confirmation, ACTOR).container.state
        == "exited"
    )
    plan = lifecycle.plan(live.slug, DEV, idle, LifecycleAction.START, ACTOR)
    assert lifecycle.confirm(live.slug, DEV, idle, plan.confirmation, ACTOR).container.running


# --- environment editor -----------------------------------------------------------


def test_env_edit_recreates_only_that_service(live: Live) -> None:
    env = live.parts.services.env
    idle_before = live.one("idle")["State"]["StartedAt"], live.cid("idle")  # type: ignore[index]
    value = 'price $5 "quoted" #hash'
    result = env.update(
        live.slug, DEV, live.cid("web"), EnvChange(set={"MODE": "fast", "EXTRA": value}), ACTOR
    )
    assert result.restarted and result.containers[0].state == "running"
    assert live.env_of("web")["MODE"] == "fast" and live.env_of("web")["EXTRA"] == value
    assert live.env_of("web")["DB_PASSWORD"] == SECRET_WORD  # compose file value survives
    assert (live.one("idle")["State"]["StartedAt"], live.cid("idle")) == idle_before  # type: ignore[index]
    listing = {i.name: i for i in env.list(live.slug, DEV, live.cid("web"))}
    assert listing["MODE"].overridden and not listing["DB_PASSWORD"].overridden
    assert env.reveal(live.slug, DEV, live.cid("web"), "EXTRA", ACTOR) == value
    path = (
        live.settings.data_dir / "overrides" / live.slug / "development" / "container-overrides.yml"
    )
    assert path.stat().st_mode & 0o777 == 0o600
    assert live.audit("container.env.update")[0]["outcome"] == "applied"
    # removing the override reverts to the compose file's value
    env.update(live.slug, DEV, live.cid("web"), EnvChange(remove=("MODE", "EXTRA")), ACTOR)
    assert live.env_of("web")["MODE"] == "ok" and "EXTRA" not in live.env_of("web")
    assert not path.exists()


def test_failed_env_edit_restores_file_and_state(live: Live) -> None:
    env = live.parts.services.env
    env.update(live.slug, DEV, live.cid("web"), EnvChange(set={"MODE": "good"}), ACTOR)
    key = EnvironmentKey(live.slug, DEV)
    path = (
        live.settings.data_dir / "overrides" / live.slug / "development" / "container-overrides.yml"
    )
    previous = path.read_bytes()
    with pytest.raises(ConflictError, match="previous configuration was restored"):
        env.update(live.slug, DEV, live.cid("web"), EnvChange(set={"MODE": "bad"}), ACTOR)
    assert path.read_bytes() == previous
    web = live.one("web")
    assert web["State"]["Running"] is True and live.env_of("web")["MODE"] == "good"  # type: ignore[index]
    assert [e["outcome"] for e in live.audit("container.env.update")][0] == "restored"
    del key


def test_failed_first_edit_removes_the_override_file(live: Live) -> None:
    path = (
        live.settings.data_dir / "overrides" / live.slug / "development" / "container-overrides.yml"
    )
    with pytest.raises(ConflictError):
        live.parts.services.env.update(
            live.slug, DEV, live.cid("web"), EnvChange(set={"MODE": "bad"}), ACTOR
        )
    assert not path.exists()
    assert live.one("web")["State"]["Running"] is True  # type: ignore[index]
    assert live.env_of("web")["MODE"] == "ok"


def test_a_stopped_service_stays_stopped_after_an_edit(live: Live) -> None:
    idle = live.cid("idle")
    assert docker("stop", "--time", "1", idle).returncode == 0
    result = live.parts.services.env.update(
        live.slug, DEV, idle, EnvChange(set={"NOTE": "x"}), ACTOR
    )
    assert not result.restarted
    record = live.one("idle")
    assert record["State"]["Status"] in {"created", "exited"} and not record["State"]["Running"]  # type: ignore[index]
    assert live.env_of("idle")["NOTE"] == "x"


# --- limits -----------------------------------------------------------------------


def test_limits_apply_live_and_survive_a_recreate(live: Live) -> None:
    web = live.cid("web")
    info = live.parts.services.limits.update(
        live.slug, DEV, web, LimitChange(cpus=0.5, memory="64m"), ACTOR
    )
    assert info.cpus == 0.5 and info.memory_bytes == 64 * 1024**2
    host = live.one("web")["HostConfig"]
    assert host["NanoCpus"] == 500_000_000 and host["Memory"] == 64 * 1024**2  # type: ignore[index]
    # a later recreate (env edit) keeps the limits because they are in the override file
    live.parts.services.env.update(live.slug, DEV, web, EnvChange(set={"A": "1"}), ACTOR)
    host = live.one("web")["HostConfig"]
    assert host["NanoCpus"] == 500_000_000 and host["Memory"] == 64 * 1024**2  # type: ignore[index]
    assert live.audit("container.limits.update")[0]["outcome"] == "applied"
    with pytest.raises(ValidationError):
        live.parts.services.limits.update(
            live.slug, DEV, live.cid("web"), LimitChange(cpus=999), ACTOR
        )


# --- files ------------------------------------------------------------------------


def test_file_browser_against_a_real_container(live: Live) -> None:
    files = live.parts.services.files
    web = live.cid("web")
    listing = files.list(live.slug, DEV, web, "/etc")
    assert "hostname" in {e.name for e in listing.entries} and "shadow" not in {
        e.name for e in listing.entries
    }
    assert "root:" in (files.read(live.slug, DEV, web, "/etc/passwd", ACTOR).text or "")
    for setup in (
        "head -c 200000 /dev/zero | tr '\\0' 'a' > /tmp/big.txt",
        "head -c 300 /dev/urandom > /tmp/random.bin",
        "ln -s /etc/passwd /tmp/link",
        "mkdir -p /tmp/adir",
    ):
        assert docker("exec", web, "sh", "-c", setup).returncode == 0
    big = files.read(live.slug, DEV, web, "/tmp/big.txt", ACTOR, max_bytes=1000)
    assert big.truncated and big.size == 200000 and len(big.text or "") == 1000
    assert files.read(live.slug, DEV, web, "/tmp/random.bin", ACTOR).binary
    with pytest.raises(ValidationError, match="symlink"):
        files.read(live.slug, DEV, web, "/tmp/link", ACTOR)
    with pytest.raises(ValidationError, match="directory"):
        files.read(live.slug, DEV, web, "/tmp/adir", ACTOR)
    with pytest.raises(NotFoundError):
        files.read(live.slug, DEV, web, "/tmp/missing", ACTOR)
    with pytest.raises(NotFoundError):
        files.list(live.slug, DEV, web, "/tmp/missing")
    assert live.audit("container.file.read")


@pytest.mark.parametrize(
    "attempt",
    ["/tmp/../etc/passwd", "/tmp/./x", "relative", "/proc/1/environ", "/sys/kernel", "/etc/shadow",
     "/root/.ssh", "/tmp//x", "/etc/passwd\n", "/tmp/a\0b"],
)  # fmt: skip
def test_traversal_attempts_never_reach_docker(live: Live, attempt: str) -> None:
    files = live.parts.services.files
    web = live.cid("web")
    with pytest.raises(ValidationError):
        files.list(live.slug, DEV, web, attempt)
    with pytest.raises(ValidationError):
        files.read(live.slug, DEV, web, attempt, ACTOR)


def test_shell_metacharacters_in_a_path_are_inert(live: Live) -> None:
    files = live.parts.services.files
    web = live.cid("web")
    for hostile in (
        "/tmp/;touch /tmp/pwned",
        "/tmp/$(touch /tmp/pwned)",
        "/tmp/`touch /tmp/pwned`",
        "/tmp/a b|c",
    ):
        with pytest.raises(NotFoundError):
            files.list(live.slug, DEV, web, hostile)
        with pytest.raises(NotFoundError):
            files.read(live.slug, DEV, web, hostile, ACTOR)
    assert docker("exec", web, "test", "-e", "/tmp/pwned").returncode != 0


# --- logs -------------------------------------------------------------------------


def test_logs_are_masked_and_follow_delivers_each_line_once(live: Live) -> None:
    logs = live.parts.services.logs
    web = live.cid("web")
    page = logs.read(live.slug, DEV, web, tail=20)
    texts = [line.text for line in page.lines]
    assert any("starting-with-[REDACTED]" in text for text in texts)
    assert SECRET_WORD not in repr(page)
    assert page.cursor is not None
    cursor: LogCursor | None = LogCursor(page.cursor.since, page.cursor.skip)
    collected: list[str] = []
    for marker in ("first-follow", "second-follow"):
        assert docker("exec", web, "sh", "-c", f"echo {marker} > /proc/1/fd/1").returncode == 0
        for _ in range(3):  # polling repeatedly must not duplicate
            page = logs.read(live.slug, DEV, web, cursor=cursor)
            collected += [line.text for line in page.lines]
            assert page.cursor is not None
            cursor = LogCursor(page.cursor.since, page.cursor.skip)
    assert collected == ["first-follow", "second-follow"]


# --- telemetry --------------------------------------------------------------------


def test_stats_and_telemetry_history(live: Live) -> None:
    telemetry = live.parts.services.telemetry
    sample, history = telemetry.container(live.slug, DEV, live.cid("web"))
    assert sample.stats.memory_used > 0 and sample.stats.memory_limit > 0 and history
    telemetry.collect_once()
    telemetry.collect_once()
    _latest, history = telemetry.container(live.slug, DEV, live.cid("web"))
    assert len(history) >= 3
    host, host_history = telemetry.host()
    assert host.memory_total > 0 and host.disk_total > 0 and host_history


# --- terminal ---------------------------------------------------------------------


def _shell_processes(live: Live, service: str) -> int:
    ps = docker("exec", live.cid(service), "ps", "-o", "args")
    return sum(1 for line in ps.stdout.splitlines() if line.strip() in {"/bin/sh", "-sh", "sh"})


def test_terminal_runs_echo_hi_and_exits(live: Live) -> None:
    client = Client(live.settings.worker_terminal_socket_path)
    client.open(slug=live.slug, container_id=live.cid("idle"))
    client.pump(until=None, timeout=3)
    assert client.terminal_id is not None
    client.send("echo hi-$((20+22))\n")
    client.pump(until=b"hi-42\r\n", timeout=10)
    assert b"hi-42\r\n" in client.output
    client.send("exit\n")
    client.pump(timeout=10)
    assert client.exit == {"code": 0, "reason": "exited"}
    assert wait_for(lambda: len(live.audit("terminal.close")) == 1)
    opened = live.audit("terminal.open")[0]
    assert opened["service"] == "idle" and opened["name"]
    assert "hi-42" not in json.dumps([live.audit("terminal.open"), live.audit("terminal.close")])
    assert _shell_processes(live, "idle") == 0


def test_terminal_idle_timeout_kills_the_exec_session(live: Live) -> None:
    client = Client(live.settings.worker_terminal_socket_path)
    client.open(slug=live.slug, container_id=live.cid("idle"))
    client.pump(timeout=3)
    client.send("echo started\n")
    client.pump(until=b"started\r\n", timeout=10)
    assert _shell_processes(live, "idle") == 1
    client.drop()  # the browser went away; idle_seconds=1 in this fixture
    assert wait_for(lambda: len(live.audit("terminal.close")) == 1, timeout=15)
    assert live.audit("terminal.close")[0]["reason"] == "idle_timeout"
    assert wait_for(lambda: _shell_processes(live, "idle") == 0, timeout=10)


def test_terminal_resize_reaches_the_container(live: Live) -> None:
    client = Client(live.settings.worker_terminal_socket_path)
    client.open(slug=live.slug, container_id=live.cid("idle"), rows=24, cols=80)
    client.pump(timeout=3)
    client.resize(40, 120)
    client.send("sleep 1; stty size\n")
    client.pump(until=b"40 120", timeout=15)
    assert b"40 120" in client.output
    client.close_terminal()
    client.pump(timeout=10)
    assert client.exit is not None and client.exit["reason"] == "closed"
    assert wait_for(lambda: _shell_processes(live, "idle") == 0, timeout=10)


def test_terminal_refuses_other_projects_and_stopped_containers(live: Live) -> None:
    stranger = docker(
        "run", "-d", "--label", "shiftcodes.test=1", "--label", f"shiftcodes.test.owner={live.owner}",
        base_image(), "sleep", "100000",
    )  # fmt: skip
    stranger_id = stranger.stdout.strip()
    try:
        client = Client(live.settings.worker_terminal_socket_path)
        client.open(slug=live.slug, container_id=stranger_id)
        client.pump(timeout=5)
        assert client.error == "container not found"
        assert docker("stop", "--time", "1", live.cid("idle")).returncode == 0
        stopped = Client(live.settings.worker_terminal_socket_path)
        stopped.open(slug=live.slug, container_id=live.cid("idle"))
        stopped.pump(timeout=5)
        assert stopped.error is not None and "start the container" in stopped.error
    finally:
        docker("rm", "-f", stranger_id)


def test_the_docker_client_class_only_uses_argv() -> None:
    """No shell: a hostile argument is passed through as one literal argument."""
    output = DockerClient().run(["version", "--format", "{{.Client.Version}};touch /tmp/never"])
    assert output.returncode == 0 and ";touch" in output.stdout
