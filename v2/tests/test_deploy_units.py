"""Compose deployer, rollback, health verification, journal, overrides and retention, against a
simulated docker (the real daemon is exercised by ``test_deploy_docker.py``)."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

import pytest

from shiftcodes.adapters.compose_deployer import ComposeDeployer, DeployConfig
from shiftcodes.adapters.compose_project import ComposeProject
from shiftcodes.adapters.container_state import ContainerInspector
from shiftcodes.adapters.deploy_health import (
    HealthPolicy,
    HealthVerifier,
    UnhealthyError,
    wait_for_status,
)
from shiftcodes.adapters.deploy_journal import DeployJournal, JournalEntry
from shiftcodes.adapters.deploy_ledger import ServiceChange
from shiftcodes.adapters.deployer import DeployRequest, HttpCheck, ServiceImage
from shiftcodes.adapters.errors import DeployError
from shiftcodes.adapters.image_retention import (
    ImageInfo,
    parse_created,
    plan_removals,
    repository_of,
)
from shiftcodes.adapters.override_store import OverrideStore
from shiftcodes.domain.enums import DeploymentStatus
from tests.deploy_support import ManualClock, MemoryLedger, NotCancelled, ScriptedDocker
from tests.execution_support import FakeProbe

LABELS = {"shiftcodes.project": "demo", "shiftcodes.environment": "development"}
FILES = ("/srv/demo/compose.yml",)
POLICY = HealthPolicy(timeout=6.0, settle=1.0, interval=0.5)


@dataclass(slots=True)
class Sim:
    """A deployer wired to the docker simulator."""

    deployer: ComposeDeployer
    docker: ScriptedDocker
    ledger: MemoryLedger
    root: Path
    probe: FakeProbe
    lines: list[str]
    old_web: str
    old_worker: str
    files: tuple[str, ...]
    env_files: tuple[str, ...]

    def request(self, *services: tuple[str, str], http: HttpCheck | None = None) -> DeployRequest:
        return DeployRequest(
            project="demo",
            environment="development",
            compose_project="demo-dev",
            compose_files=self.files,
            compose_env_files=self.env_files,
            services=tuple(ServiceImage(s, i) for s, i in services),
            run_id=7,
            commit_sha="a" * 40,
            http_check=http,
        )

    def deploy(self, request: DeployRequest) -> None:
        self.deployer.deploy(request, self.lines.append, NotCancelled())

    def new_image(self, tag: str = "sc/demo-development-web:aaa-r2") -> str:
        self.docker.add_image(tag, LABELS)
        return tag

    @property
    def overrides(self) -> OverrideStore:
        return OverrideStore(self.root)


def build_sim(tmp_path: Path, *, keep: int = 5) -> Sim:
    docker = ScriptedDocker({"web": "app-web:1", "worker": "app-worker:1"})
    old_web = docker.add_image("app-web:1", LABELS)
    old_worker = docker.add_image("app-worker:1", LABELS)
    docker.start("web", "app-web:1", "healthy")
    docker.start("worker", "app-worker:1", "healthy")
    ledger = MemoryLedger()
    probe = FakeProbe([200])
    deployer = ComposeDeployer(
        docker,
        ledger,
        tmp_path / "deployments",
        probe,
        config=DeployConfig(health=POLICY, keep_images=keep),
        clock=ManualClock(),
    )
    compose = tmp_path / "compose.yml"
    env = tmp_path / "ports.env"
    compose.write_text("services: {}\n")
    env.write_text("WEB_PORT=8080\n")
    return Sim(
        deployer,
        docker,
        ledger,
        tmp_path / "deployments",
        probe,
        [],
        old_web,
        old_worker,
        (str(compose),),
        (str(env),),
    )


@pytest.fixture
def sim(tmp_path: Path) -> Sim:
    return build_sim(tmp_path)


# --- successful deployment -------------------------------------------------------------------


def test_deploys_only_the_requested_service_and_records_the_previous_image(sim: Sim) -> None:
    worker_before = sim.docker.containers["worker"]
    new = sim.new_image()
    sim.deploy(sim.request(("web", new)))
    assert sim.docker.up_services == [["web"]]
    assert sim.docker.containers["worker"] is worker_before
    assert sim.docker.containers["web"].image_ref == new
    row = sim.ledger.last
    assert row.status is DeploymentStatus.SUCCEEDED
    change = row.changes[0]
    assert (change.service, change.previous_image, change.previous_image_id) == (
        "web",
        "app-web:1",
        sim.old_web,
    )
    assert change.new_image == new
    assert change.new_image_id == sim.docker.image_id(new)
    assert not DeployJournal(sim.root).exists("demo-dev")


def test_up_never_pulls_builds_or_follows_dependencies(sim: Sim) -> None:
    sim.deploy(sim.request(("web", sim.new_image())))
    up = next(c for c in sim.docker.calls if "up" in c)
    assert {"--no-deps", "--no-build", "--detach"} <= set(up)
    assert up[up.index("--pull") + 1] == "never"
    assert up[:5] == ["compose", "--env-file", sim.env_files[0], "-p", "demo-dev"]
    assert up.count("-f") == 2
    assert up[up.index("-f", up.index("-f") + 1) + 1].endswith("override.yml")


def test_managed_override_keeps_other_services_and_the_previous_version(sim: Sim) -> None:
    first = sim.new_image("sc/demo-development-web:aaa-r2")
    sim.deploy(sim.request(("web", first)))
    worker = sim.new_image("sc/demo-development-worker:bbb-r3")
    sim.deploy(sim.request(("worker", worker)))
    assert sim.overrides.current("demo-dev") == {"web": first, "worker": worker}
    assert sim.overrides.previous("demo-dev") == {"web": first}


def test_a_service_that_was_not_running_has_no_previous_image(sim: Sim) -> None:
    del sim.docker.containers["worker"]
    sim.deploy(sim.request(("worker", sim.new_image("sc/demo-development-worker:x-r1"))))
    change = sim.ledger.last.changes[0]
    assert change.previous_image is None
    assert change.previous_image_id is None


# --- refusals before anything changes --------------------------------------------------------


@pytest.mark.parametrize(
    ("services", "message"),
    [
        ((("db", "x"),), "not defined in the Compose project: db"),
        ((("web", "missing:1"),), "is not present locally"),
    ],
)
def test_refusals_change_nothing(
    sim: Sim, services: tuple[tuple[str, str], ...], message: str
) -> None:
    with pytest.raises(DeployError, match=message):
        sim.deploy(sim.request(*services))
    assert sim.docker.up_services == []
    assert sim.ledger.last.status is DeploymentStatus.FAILED
    assert not DeployJournal(sim.root).exists("demo-dev")
    assert not sim.overrides.path("demo-dev").exists()


def test_environment_without_compose_configuration_is_refused(sim: Sim) -> None:
    request = DeployRequest(
        "demo", "development", None, (), (ServiceImage("web", "x"),), 1, "a" * 40
    )
    with pytest.raises(DeployError, match="no Compose project"):
        sim.deployer.deploy(request, sim.lines.append, NotCancelled())


def test_cancel_before_the_change_leaves_everything_alone(sim: Sim) -> None:
    class Cancelled:
        def is_set(self) -> bool:
            return True

    request = sim.request(("web", sim.new_image()))
    with pytest.raises(DeployError, match="cancelled"):
        sim.deployer.deploy(request, sim.lines.append, Cancelled())
    assert sim.docker.up_services == []
    assert not DeployJournal(sim.root).exists("demo-dev")


def test_a_journal_that_was_not_recovered_blocks_the_environment(sim: Sim) -> None:
    sim.deploy(sim.request(("web", sim.new_image())))
    entry = JournalEntry(1, 7, "demo", "development", "demo-dev", sim.files, {}, ())
    DeployJournal(sim.root).write(entry)
    with pytest.raises(DeployError, match="not been recovered"):
        sim.deploy(sim.request(("web", sim.new_image("sc/demo-development-web:ccc-r4"))))


# --- failure and rollback --------------------------------------------------------------------


@pytest.mark.parametrize("behaviour", ["unhealthy", "exited", "restarting"])
def test_unhealthy_new_containers_are_rolled_back(sim: Sim, behaviour: str) -> None:
    new = sim.new_image()
    sim.docker.behaviour[new] = behaviour
    with pytest.raises(DeployError, match="rolled back to the previous images"):
        sim.deploy(sim.request(("web", new)))
    assert sim.docker.containers["web"].image_id == sim.old_web
    assert sim.ledger.last.status is DeploymentStatus.ROLLED_BACK
    assert sim.overrides.current("demo-dev") == {}
    assert not DeployJournal(sim.root).exists("demo-dev")
    assert sim.docker.up_services == [["web"], ["web"]]


def test_rollback_restores_the_override_of_the_earlier_deployment(sim: Sim) -> None:
    good = sim.new_image("sc/demo-development-web:aaa-r2")
    sim.deploy(sim.request(("web", good)))
    bad = sim.new_image("sc/demo-development-web:bbb-r3")
    sim.docker.behaviour[bad] = "unhealthy"
    with pytest.raises(DeployError, match="rolled back"):
        sim.deploy(sim.request(("web", bad)))
    assert sim.overrides.current("demo-dev") == {"web": good}
    assert sim.docker.containers["web"].image_ref == good


def test_failed_rollback_is_surfaced(sim: Sim) -> None:
    new = sim.new_image()
    sim.docker.behaviour[new] = "unhealthy"
    sim.docker.behaviour["app-web:1"] = "unhealthy"
    with pytest.raises(DeployError, match="ROLLBACK FAILED"):
        sim.deploy(sim.request(("web", new)))
    assert sim.ledger.last.status is DeploymentStatus.ROLLBACK_FAILED
    assert "manual intervention" in (sim.ledger.last.message or "")
    assert not DeployJournal(sim.root).exists("demo-dev")


def test_failing_compose_up_is_rolled_back(sim: Sim) -> None:
    sim.docker.up_exit_code = 1
    with pytest.raises(DeployError):
        sim.deploy(sim.request(("web", sim.new_image())))
    assert sim.ledger.last.status is DeploymentStatus.ROLLBACK_FAILED  # the rollback up fails too
    sim.docker.up_exit_code = 0
    assert sim.docker.containers["web"].image_id == sim.old_web


def test_first_deployment_of_a_service_is_rolled_back_by_removing_it(sim: Sim) -> None:
    del sim.docker.containers["worker"]
    new = sim.new_image("sc/demo-development-worker:x-r1")
    sim.docker.behaviour[new] = "unhealthy"
    with pytest.raises(DeployError, match="rolled back"):
        sim.deploy(sim.request(("worker", new)))
    assert "worker" not in sim.docker.containers
    assert sim.overrides.current("demo-dev") == {}


def test_failing_http_check_rolls_back(sim: Sim) -> None:
    sim.probe.results = [500]
    new = sim.new_image()
    request = sim.request(("web", new), http=HttpCheck("http://app.test/up", 200, 2))
    with pytest.raises(DeployError, match="health check failed"):
        sim.deploy(request)
    assert sim.docker.containers["web"].image_id == sim.old_web
    assert sim.ledger.last.status is DeploymentStatus.ROLLED_BACK


def test_http_check_that_passes_succeeds(sim: Sim) -> None:
    sim.probe.results = [503, 200]
    request = sim.request(("web", sim.new_image()), http=HttpCheck("http://app.test/up", 200, 30))
    sim.deploy(request)
    assert sim.ledger.last.status is DeploymentStatus.SUCCEEDED


def test_rollback_pins_the_image_id_when_the_previous_tag_moved(sim: Sim) -> None:
    new = sim.new_image()
    sim.docker.behaviour[new] = "unhealthy"
    sim.docker.images[sim.old_web].tags.remove("app-web:1")
    sim.docker.add_image("app-web:1", LABELS)  # the tag now points at a different image
    with pytest.raises(DeployError, match="rolled back"):
        sim.deploy(sim.request(("web", new)))
    assert sim.overrides.current("demo-dev") == {"web": sim.old_web}
    assert sim.docker.containers["web"].image_id == sim.old_web


# --- crash recovery --------------------------------------------------------------------------


def test_recovery_rolls_back_an_interrupted_deployment(sim: Sim) -> None:
    new = sim.new_image()
    deployment_id = sim.ledger.begin("demo", "development", 7, "a" * 40)
    changes = (ServiceChange("web", new, sim.docker.image_id(new), "app-web:1", sim.old_web),)
    sim.ledger.record_services(deployment_id, changes)
    entry = JournalEntry(
        deployment_id, 7, "demo", "development", "demo-dev", sim.files, {}, changes, sim.env_files
    )
    DeployJournal(sim.root).write(entry)
    sim.overrides.write("demo-dev", {"web": new})
    sim.docker.start("web", new, "healthy")  # the worker died after compose up
    assert sim.deployer.recover() == [deployment_id]
    assert sim.docker.containers["web"].image_id == sim.old_web
    assert sim.ledger.rows[deployment_id].status is DeploymentStatus.ROLLED_BACK
    assert "interrupted" in (sim.ledger.rows[deployment_id].message or "")
    assert sim.overrides.current("demo-dev") == {}
    assert not DeployJournal(sim.root).exists("demo-dev")
    assert sim.deployer.recover() == []


def test_recovery_fails_deployments_that_never_reached_the_journal(sim: Sim) -> None:
    deployment_id = sim.ledger.begin("demo", "development", 7, "a" * 40)
    assert sim.deployer.recover() == [deployment_id]
    assert sim.ledger.rows[deployment_id].status is DeploymentStatus.FAILED


# --- image retention after a deployment ------------------------------------------------------


def test_retention_keeps_newest_protected_running_and_foreign_images(tmp_path: Path) -> None:
    sim = build_sim(tmp_path, keep=2)
    repo = "sc/demo-development-web"
    docker = sim.docker
    for number in range(1, 6):
        docker.add_image(
            f"{repo}:s{number}-r{number}", LABELS, created=f"2026-10-13T11:0{number}:00Z"
        )
    foreign = docker.add_image("nginx:latest")
    other_env = docker.add_image(
        "sc/demo-production-web:old-r1",
        {"shiftcodes.project": "demo", "shiftcodes.environment": "production"},
        created="2026-10-01T00:00:00Z",
    )
    sim.deploy(sim.request(("web", f"{repo}:s5-r5")))
    remaining = {t for i in docker.images.values() for t in i.tags}
    assert {f"{repo}:s5-r5", f"{repo}:s4-r4"} <= remaining
    assert not {f"{repo}:s3-r3", f"{repo}:s2-r2", f"{repo}:s1-r1"} & remaining
    assert docker.image_id("nginx:latest") == foreign
    assert docker.image_id("sc/demo-production-web:old-r1") == other_env
    assert "app-web:1" in remaining  # previous image of the last deployment


def test_retention_never_touches_images_used_by_a_container(tmp_path: Path) -> None:
    sim = build_sim(tmp_path, keep=1)
    repo = "sc/demo-development-web"
    sim.docker.add_image(f"{repo}:old", LABELS, created="2026-10-01T00:00:00Z")
    sim.docker.start("worker", f"{repo}:old")
    sim.docker.add_image(f"{repo}:mid", LABELS, created="2026-10-02T00:00:00Z")
    sim.docker.add_image(f"{repo}:new", LABELS, created="2026-10-03T00:00:00Z")
    sim.deploy(sim.request(("web", f"{repo}:new")))
    assert sim.docker.image_id(f"{repo}:old") is not None
    assert sim.docker.image_id(f"{repo}:mid") is None


# --- building blocks -------------------------------------------------------------------------


def test_override_store_writes_atomically_and_keeps_the_previous_version(tmp_path: Path) -> None:
    store = OverrideStore(tmp_path)
    assert store.current("p") == {}
    assert store.file_for_compose("p") is None
    store.write("p", {"web": "a:1"})
    store.write("p", {"web": "a:2", "db": "d:1"})
    assert store.current("p") == {"db": "d:1", "web": "a:2"}
    assert store.previous("p") == {"web": "a:1"}
    document = json.loads(store.path("p").read_text())
    assert document == {"services": {"db": {"image": "d:1"}, "web": {"image": "a:2"}}}
    assert sorted(f.name for f in store.path("p").parent.iterdir()) == [
        "override.yml",
        "override.yml.prev",
    ]


def test_unreadable_override_is_an_error(tmp_path: Path) -> None:
    store = OverrideStore(tmp_path)
    store.write("p", {"web": "a:1"})
    store.path("p").write_text("{ not json")
    with pytest.raises(DeployError, match="unreadable"):
        store.current("p")


def test_journal_round_trip_and_corrupt_files_are_set_aside(tmp_path: Path) -> None:
    journal = DeployJournal(tmp_path)
    change = ServiceChange("web", "n:2", "sha256:n", "n:1", "sha256:o")
    entry = JournalEntry(
        3, 9, "demo", "production", "demo-prod", FILES, {"web": "n:1"}, (change,), ("/e/a.env",)
    )
    journal.write(entry)
    assert journal.exists("demo-prod")
    assert list(journal.pending()) == [entry]
    journal.path("bad").parent.mkdir()
    journal.path("bad").write_text("{")
    assert list(journal.pending()) == [entry]
    assert journal.path("bad").with_name("journal.json.corrupt").exists()
    journal.remove("demo-prod")
    assert not journal.exists("demo-prod")


def test_parse_created_and_repository_of() -> None:
    assert parse_created("2026-10-13T10:00:00.123456789Z").microsecond == 123456
    assert parse_created("2026-10-13T10:00:00Z") < parse_created("2026-10-13T10:00:00.5Z")
    assert parse_created("garbage").year == 1
    assert repository_of("sc/p-e-web:abc-r3") == "sc/p-e-web"
    assert repository_of("localhost:5000/app:1") == "localhost:5000/app"
    assert repository_of("localhost:5000/app") == "localhost:5000/app"


def test_plan_removals_groups_by_repository() -> None:
    def image(ident: str, tag: str, day: int) -> ImageInfo:
        return ImageInfo(ident, (tag,), parse_created(f"2026-10-{day:02d}T00:00:00Z"))

    images = [
        image("a", "r/web:1", 1),
        image("b", "r/web:2", 2),
        image("c", "r/web:3", 3),
        image("d", "r/api:1", 1),
    ]
    assert plan_removals(images, 2, frozenset()) == [("a", "r/web:1")]
    assert plan_removals(images, 1, frozenset({"b"})) == [("a", "r/web:1")]
    assert plan_removals(images, 1, frozenset({"r/web:1"})) == [("b", "r/web:2")]
    assert plan_removals(images, 5, frozenset()) == []


# --- health verification ---------------------------------------------------------------------


def _verifier(docker: ScriptedDocker) -> tuple[HealthVerifier, ComposeProject]:
    inspector = ContainerInspector(docker)
    verifier = HealthVerifier(inspector, POLICY, FakeProbe([200]), ManualClock())
    return verifier, ComposeProject(docker, "p", FILES)


def test_verifier_waits_for_a_starting_container_to_turn_healthy() -> None:
    docker = ScriptedDocker({"web": "w:1"})
    docker.add_image("w:1")
    container = docker.start("web", "w:1", "starting")
    verifier, project = _verifier(docker)
    calls = iter(range(3))

    class Flip(ManualClock):
        def sleep(self, seconds: float) -> None:
            super().sleep(seconds)
            if next(calls, 99) == 1:
                container.health = "healthy"

    verifier = HealthVerifier(ContainerInspector(docker), POLICY, FakeProbe([200]), Flip())
    lines: list[str] = []
    verifier.verify(project, ["web"], lines.append)
    assert lines[-1] == "healthy: web"


def test_verifier_times_out_with_the_last_problem() -> None:
    docker = ScriptedDocker({"web": "w:1"})
    docker.add_image("w:1")
    docker.start("web", "w:1", "starting")
    verifier, project = _verifier(docker)
    with pytest.raises(UnhealthyError, match=r"web is starting"):
        verifier.verify(project, ["web"], lambda _line: None)


def test_verifier_detects_a_restart() -> None:
    docker = ScriptedDocker({"web": "w:1"})
    docker.add_image("w:1")
    container = docker.start("web", "w:1", "healthy")
    inspector = ContainerInspector(docker)

    class Bump(ManualClock):
        def sleep(self, seconds: float) -> None:
            super().sleep(seconds)
            container.restarts += 1

    verifier = HealthVerifier(inspector, HealthPolicy(6.0, 3.0, 0.5), FakeProbe([200]), Bump())
    with pytest.raises(UnhealthyError, match="restarted"):
        verifier.verify(ComposeProject(docker, "p", FILES), ["web"], lambda _line: None)


def test_verifier_fails_when_there_is_no_container() -> None:
    docker = ScriptedDocker({"web": "w:1"})
    verifier, project = _verifier(docker)
    with pytest.raises(UnhealthyError, match="no container"):
        verifier.verify(project, ["web"], lambda _line: None)


def test_wait_for_status_retries_then_fails() -> None:
    lines: list[str] = []
    probe = FakeProbe([None, 503, 200])
    wait_for_status(probe, HttpCheck("http://x/", 200, 10), 1.0, lines.append, ManualClock())
    assert lines[-1] == "health check passed: 200"
    with pytest.raises(UnhealthyError, match="got status 503"):
        wait_for_status(
            FakeProbe([503]), HttpCheck("http://x/", 200, 3), 1.0, lines.append, ManualClock()
        )


# --- compose env files and readable paths ----------------------------------------------------


def test_every_compose_invocation_gets_the_env_files(sim: Sim) -> None:
    new = sim.new_image()
    sim.docker.behaviour[new] = "unhealthy"
    with pytest.raises(DeployError, match="rolled back"):
        sim.deploy(sim.request(("web", new)))
    compose_calls = [c for c in sim.docker.calls if c[0] == "compose"]
    assert {"config", "ps", "up"} <= {w for c in compose_calls for w in c}
    for call in compose_calls:
        assert call[1:3] == ["--env-file", sim.env_files[0]], call
        assert call[3:5] == ["-p", "demo-dev"]


def test_recovery_uses_the_env_files_of_the_journal(sim: Sim) -> None:
    new = sim.new_image()
    deployment_id = sim.ledger.begin("demo", "development", 7, "a" * 40)
    changes = (ServiceChange("web", new, sim.docker.image_id(new), "app-web:1", sim.old_web),)
    entry = JournalEntry(
        deployment_id, 7, "demo", "development", "demo-dev", sim.files, {}, changes, sim.env_files
    )
    DeployJournal(sim.root).write(entry)
    sim.docker.calls.clear()
    sim.deployer.recover()
    compose_calls = [c for c in sim.docker.calls if c[0] == "compose"]
    assert compose_calls
    assert all(c[1:3] == ["--env-file", sim.env_files[0]] for c in compose_calls)


def test_compose_project_base_command_is_the_single_argv_builder(tmp_path: Path) -> None:
    docker = ScriptedDocker({})
    override = tmp_path / "override.yml"
    project = ComposeProject(docker, "p", ("/a.yml", "/b.yml"), override, ("/x.env", "/y.env"))
    assert project.base_command() == [
        "compose",
        "--env-file",
        "/x.env",
        "--env-file",
        "/y.env",
        "-p",
        "p",
        "-f",
        "/a.yml",
        "-f",
        "/b.yml",
        "-f",
        str(override),
    ]
    assert project.base_command(with_override=False)[-2:] == ["-f", "/b.yml"]
    assert ComposeProject(docker, "p", ("/a.yml",)).base_command() == [
        "compose",
        "-p",
        "p",
        "-f",
        "/a.yml",
    ]


@pytest.mark.parametrize("which", ["compose", "env"])
def test_unreadable_compose_or_env_files_are_refused_before_anything_happens(
    sim: Sim, which: str
) -> None:
    request = sim.request(("web", sim.new_image()))
    if which == "compose":
        request = replace(request, compose_files=("/nonexistent/compose.yml",))
    else:
        request = replace(request, compose_env_files=("/nonexistent/ports.env",))
    with pytest.raises(DeployError, match="not readable by the worker"):
        sim.deploy(request)
    assert sim.docker.up_services == []
    assert not sim.ledger.rows
