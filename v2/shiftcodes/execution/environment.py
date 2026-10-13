"""The worker-wide collaborators of execution, wired once."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from shiftcodes.adapters.container_runtime import ContainerRuntime
from shiftcodes.adapters.deployer import Deployer
from shiftcodes.adapters.http_probe import HttpProbe
from shiftcodes.adapters.sources import SourceProvider
from shiftcodes.adapters.workspace import WorkspaceManager
from shiftcodes.domain.clock import Clock, utcnow
from shiftcodes.execution.config import ExecutionConfig
from shiftcodes.execution.logs import LogStore
from shiftcodes.repositories.database import SessionFactory, UnitOfWork
from shiftcodes.services.run_lifecycle import RunLifecycleService
from shiftcodes.services.run_progress import RunRecorder
from shiftcodes.services.secrets import SecretService


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

    def uow(self) -> UnitOfWork:
        """A fresh unit of work."""
        return UnitOfWork(self.session_factory)

    def lifecycle(self) -> RunLifecycleService:
        """A lifecycle service with its own unit of work."""
        return RunLifecycleService(self.uow(), self.clock)

    def recorder(self) -> RunRecorder:
        """A recorder (it opens a unit of work per call)."""
        return RunRecorder(self.uow, self.clock)
