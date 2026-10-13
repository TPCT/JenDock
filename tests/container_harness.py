"""A fully wired container-management stack on fakes, shared by service and API tests."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Engine

from backend.adapters.compose_overrides import ComposeOverrideStore
from backend.containers.base import ContainerAuditor, Membership, TargetResolver
from backend.containers.config import ContainerConfig
from backend.containers.confirmations import ConfirmationStore
from backend.containers.environment import EnvironmentEditor
from backend.containers.files import FileBrowser
from backend.containers.lifecycle import ContainerInventory, LifecycleService
from backend.containers.limits import LimitsService
from backend.containers.locks import EnvironmentLocks
from backend.containers.logs import LogService
from backend.containers.recreate import ServiceRecreator, StoreOverrideFiles
from backend.containers.telemetry import HostSampler, TelemetryService
from backend.domain.enums import EnvironmentName
from backend.repositories.database import UnitOfWork, create_session_factory
from backend.services.environments import EnvironmentService, EnvironmentUpdate
from backend.worker.container_handlers import ContainerServices
from tests.container_support import FakeCompose, FakeEngine, make_record

PROJECT = "demo-dev"
DEV = EnvironmentName.DEVELOPMENT
FAST = ContainerConfig(
    max_cpus=4.0,
    max_memory_bytes=2 * 1024**3,
    verify_timeout=1.5,
    lock_timeout=0.4,
    settle_seconds=0.0,
    poll_seconds=0.01,
)


@dataclass
class Stack:
    """Everything a test needs: fakes, services, the store and the lock registry."""

    engine: FakeEngine
    compose: FakeCompose
    store: ComposeOverrideStore
    locks: EnvironmentLocks
    confirmations: ConfirmationStore
    services: ContainerServices
    slug: str
    web_id: str
    other_id: str
    foreign_id: str


def build_stack(tmp_path: Path, db: Engine, slug: str, config: ContainerConfig = FAST) -> Stack:
    """Configure the environment's compose project and wire services onto fakes."""
    factory = create_session_factory(db)
    compose_file = tmp_path / "repo" / "compose.yml"
    compose_file.parent.mkdir(parents=True, exist_ok=True)
    compose_file.write_text("services: {}\n")
    EnvironmentService(UnitOfWork(factory)).update(
        slug,
        DEV,
        EnvironmentUpdate(compose_project=PROJECT, compose_files=[str(compose_file)]),
    )
    engine = FakeEngine()
    web = engine.add(
        make_record(service="web", env={"PATH": "/bin", "DB_PASSWORD": "hunter2-secret"})
    )
    other = engine.add(make_record(service="worker", running=False))
    foreign = engine.add(make_record(project="someone-else", service="web"))
    compose = FakeCompose(engine)
    store = ComposeOverrideStore(tmp_path / "overrides")
    locks = EnvironmentLocks()
    confirmations = ConfirmationStore()
    resolver = TargetResolver(factory)
    membership = Membership(engine)
    auditor = ContainerAuditor(factory)
    recreator = ServiceRecreator(engine, compose, StoreOverrideFiles(store), config)
    telemetry = TelemetryService(
        HostSampler(tmp_path), engine, resolver, membership, lambda: [PROJECT], 10
    )
    services = ContainerServices(
        inventory=ContainerInventory(resolver, engine, membership),
        lifecycle=LifecycleService(
            resolver, engine, membership, locks, confirmations, auditor, config
        ),
        logs=LogService(resolver, engine, membership),
        env=EnvironmentEditor(resolver, membership, store, recreator, locks, auditor, config),
        limits=LimitsService(resolver, engine, membership, store, locks, auditor, config),
        files=FileBrowser(resolver, engine, membership, auditor),
        telemetry=telemetry,
    )
    return Stack(engine, compose, store, locks, confirmations, services, slug, web, other, foreign)
