"""The worker-wide collaborators of execution, wired once."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from backend.adapters.container_runtime import ContainerRuntime
from backend.adapters.deployer import Deployer
from backend.adapters.http_probe import HttpProbe
from backend.adapters.sources import SourceProvider
from backend.adapters.workspace import WorkspaceManager
from backend.domain.clock import Clock, utcnow
from backend.execution.config import ExecutionConfig
from backend.execution.logs import LogStore
from backend.execution.status_reporter import NoStatusReporter, StatusReporter
from backend.repositories.database import SessionFactory, UnitOfWork
from backend.services.notification_events import NoRunNotifications, RunNotifications
from backend.services.run_lifecycle import RunLifecycleService
from backend.services.run_progress import RunRecorder
from backend.services.secrets import SecretService


@dataclass(frozen=True, slots=True)
class ExecutionEnv:
    """Everything a worker needs to execute runs. Services are built per use because each
    holds a non-reentrant unit of work."""

    config: ExecutionConfig
    runtime: ContainerRuntime
    source: SourceProvider
    deployer: Deployer
    probe: HttpProbe
    workspaces: WorkspaceManager
    logs: LogStore
    session_factory: SessionFactory
    secret_service: Callable[[], SecretService]
    clock: Clock = utcnow
    status: StatusReporter = field(default_factory=NoStatusReporter)
    notifications: RunNotifications = field(default_factory=NoRunNotifications)

    def uow(self) -> UnitOfWork:
        """A fresh unit of work."""
        return UnitOfWork(self.session_factory)

    def lifecycle(self) -> RunLifecycleService:
        """A lifecycle service with its own unit of work."""
        return RunLifecycleService(self.uow(), self.clock)

    def recorder(self) -> RunRecorder:
        """A recorder (it opens a unit of work per call)."""
        return RunRecorder(self.uow, self.clock)
