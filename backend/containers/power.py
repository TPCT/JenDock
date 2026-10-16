"""Environment-wide start and stop operations."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from backend.adapters.compose_project import ComposeProject, ensure_readable
from backend.adapters.docker_commands import CommandRunner
from backend.containers.base import ContainerAuditor, EnvironmentTarget, TargetResolver
from backend.containers.lifecycle import ContainerInventory
from backend.containers.locks import EnvironmentLocks, lock_key
from backend.domain.actor import Actor
from backend.domain.clock import Clock, utcnow
from backend.domain.enums import EnvironmentName
from backend.domain.errors import ConflictError
from backend.repositories.database import SessionFactory, UnitOfWork
from backend.services.lookup import require_environment


class PowerState(StrEnum):
    """Environment runtime state."""

    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    NOT_DEPLOYED = "NOT_DEPLOYED"


@dataclass(frozen=True, slots=True)
class EnvironmentPower:
    """Current state and timestamps used to schedule an automatic stop."""

    state: PowerState
    running: int
    total: int
    auto_stop_hours: int | None
    last_deployed_at: datetime | None
    started_at: datetime | None
    auto_stop_due_at: datetime | None


class EnvironmentPowerService:
    """Controls one complete Compose project while sharing deployment/container locks."""

    def __init__(  # noqa: PLR0913, PLR0917 - collaborators of this use case
        self,
        resolver: TargetResolver,
        inventory: ContainerInventory,
        commands: CommandRunner,
        compose_files: Callable[[EnvironmentTarget], Sequence[str]],
        locks: EnvironmentLocks,
        auditor: ContainerAuditor,
        session_factory: SessionFactory,
        clock: Clock = utcnow,
    ) -> None:
        self._resolver = resolver
        self._inventory = inventory
        self._commands = commands
        self._compose_files = compose_files
        self._locks = locks
        self._auditor = auditor
        self._sessions = session_factory
        self._clock = clock

    def state(self, slug: str, env: EnvironmentName) -> EnvironmentPower:
        """Compute state from all containers, including exited containers."""
        uow = UnitOfWork(self._sessions)
        with uow:
            row = require_environment(uow, slug, env)
            environment_id = row.id
            compose_project = row.compose_project
            started_at, auto_stop_hours = row.started_at, row.auto_stop_hours
        infos = self._inventory.list(slug, env) if compose_project else []
        running = sum(info.running for info in infos)
        state = (
            PowerState.NOT_DEPLOYED
            if not infos
            else (PowerState.RUNNING if running else PowerState.STOPPED)
        )
        with uow:
            deployment = uow.deployments.latest_successful(environment_id)
            last_deployed_at = deployment.finished_at if deployment else None
        ref = max((x for x in (started_at, last_deployed_at) if x is not None), default=None)
        due = ref + timedelta(hours=auto_stop_hours) if ref and auto_stop_hours else None
        return EnvironmentPower(
            state, running, len(infos), auto_stop_hours, last_deployed_at, started_at, due
        )

    def stop(  # noqa: PLR0913 - stop exposes optional scheduler guard and audit context
        self,
        slug: str,
        env: EnvironmentName,
        actor: Actor,
        *,
        audit_action: str = "environment.stop",
        audit_details: dict[str, object] | None = None,
        only_if: Callable[[EnvironmentPower], bool] | None = None,
    ) -> EnvironmentPower:
        """Stop every service while preserving the project and its volumes."""
        return self._change(slug, env, actor, "stop", audit_action, audit_details, only_if)

    def start(self, slug: str, env: EnvironmentName, actor: Actor) -> EnvironmentPower:
        """Start services with existing containers using deployed images without pulling."""
        return self._change(slug, env, actor, "start", "environment.start", None, None)

    def _change(  # noqa: PLR0913, PLR0917 - one power operation and its audit context
        self,
        slug: str,
        env: EnvironmentName,
        actor: Actor,
        action: str,
        audit_action: str,
        audit_details: dict[str, object] | None,
        only_if: Callable[[EnvironmentPower], bool] | None,
    ) -> EnvironmentPower:
        target = self._resolver.resolve(slug, env)
        before = EnvironmentPower(PowerState.NOT_DEPLOYED, 0, 0, None, None, None, None)
        try:
            ensure_readable(target.compose_files, target.compose_env_files)
            with self._locks.hold(lock_key(slug, env.value), timeout=30):
                before = self.state(slug, env)
                if only_if is not None and not only_if(before):
                    return before
                if action == "start" and before.state is PowerState.NOT_DEPLOYED:
                    raise ConflictError("deploy it first")
                services = sorted(
                    {
                        info.service
                        for info in self._inventory.list(slug, env)
                        if info.service is not None
                    }
                )
                if action == "start" and not services:
                    raise ConflictError("deploy it first")
                files = (*target.compose_files, *self._compose_files(target))
                project = ComposeProject(
                    self._commands,
                    target.compose_project,
                    files,
                    env_files=target.compose_env_files,
                )
                result = (
                    project.stop(lambda _line: None, timeout=180)
                    if action == "stop"
                    else project.start(services, lambda _line: None, timeout=180)
                )
                if not result.ok:
                    raise ConflictError("Compose could not change the environment state")
                if action == "start":
                    uow = UnitOfWork(self._sessions)
                    with uow:
                        row = uow.environments.get(target.environment_id)
                        if row is None:
                            raise ConflictError("environment no longer exists")
                        row.started_at = self._clock()
            self._auditor.record_environment(
                actor,
                audit_action,
                target,
                {"previous_state": before.state.value, "outcome": "done", **(audit_details or {})},
            )
            return self.state(slug, env)
        except Exception:
            self._auditor.record_environment(
                actor,
                audit_action,
                target,
                {
                    "previous_state": before.state.value,
                    "outcome": "failed",
                    **(audit_details or {}),
                },
            )
            raise
