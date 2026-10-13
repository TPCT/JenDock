"""Inventory and the two-step start/stop/restart of containers."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from backend.adapters.container_admin import ContainerEngine
from backend.adapters.docker_inspect import parse_container
from backend.adapters.errors import ContainerRuntimeError
from backend.containers.base import (
    ContainerAuditor,
    EnvironmentTarget,
    Membership,
    TargetResolver,
)
from backend.containers.config import ContainerConfig
from backend.containers.confirmations import ConfirmationStore
from backend.containers.locks import EnvironmentLocks
from backend.domain.actor import Actor
from backend.domain.containers import ContainerInfo, LifecycleAction
from backend.domain.enums import EnvironmentName
from backend.domain.errors import ConflictError, ValidationError

logger = logging.getLogger(__name__)

_EFFECTS = {
    LifecycleAction.START: "The container is started; its main process begins running again.",
    LifecycleAction.STOP: (
        "The container receives a stop signal and is killed after 10 seconds if it does not exit; "
        "whatever it serves is unavailable until it is started again."
    ),
    LifecycleAction.RESTART: (
        "The container is stopped (10 second grace) and started again; connections are dropped "
        "and it is briefly unavailable."
    ),
}


@dataclass(frozen=True, slots=True)
class ActionPlan:
    """Step one: what would happen, plus the single-use confirmation that executes it."""

    action: LifecycleAction
    container: ContainerInfo
    effects: tuple[str, ...]
    confirmation: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class ActionResult:
    """Step two: the container after the action."""

    action: LifecycleAction
    previous_state: str
    container: ContainerInfo


class ContainerInventory:
    """Lists and describes the containers of an environment's compose project."""

    def __init__(
        self, resolver: TargetResolver, engine: ContainerEngine, membership: Membership
    ) -> None:
        self._resolver = resolver
        self._engine = engine
        self._membership = membership

    def list(self, slug: str, env: EnvironmentName) -> list[ContainerInfo]:
        """Every container of the project (any state), ordered by service then name."""
        target = self._resolver.resolve(slug, env)
        try:
            records = self._engine.list_project(target.compose_project)
        except ContainerRuntimeError as error:
            raise ConflictError(str(error)) from error
        infos = [parse_container(record) for record in records]
        return sorted(infos, key=lambda info: (info.service or "", info.name))

    def get(self, slug: str, env: EnvironmentName, container_id: str) -> ContainerInfo:
        """One container, by its exact id."""
        target = self._resolver.resolve(slug, env)
        return parse_container(self._membership.require(target, container_id))


class LifecycleService:
    """Plan then confirm: nothing happens until the exact confirmation comes back."""

    def __init__(  # noqa: PLR0913, PLR0917 - collaborators of one use case
        self,
        resolver: TargetResolver,
        engine: ContainerEngine,
        membership: Membership,
        locks: EnvironmentLocks,
        confirmations: ConfirmationStore,
        auditor: ContainerAuditor,
        config: ContainerConfig,
    ) -> None:
        self._resolver = resolver
        self._engine = engine
        self._membership = membership
        self._locks = locks
        self._confirmations = confirmations
        self._auditor = auditor
        self._config = config

    def plan(
        self,
        slug: str,
        env: EnvironmentName,
        container_id: str,
        action: LifecycleAction,
        actor: Actor,
    ) -> ActionPlan:
        """Describe the action and issue its confirmation; nothing is changed."""
        target = self._resolver.resolve(slug, env)
        info = parse_container(self._membership.require(target, container_id))
        self._check_applicable(info, action)
        effects = (
            f"{action.value.capitalize()} container {info.name} (service {info.service or '-'}, "
            f"currently {info.state}).",
            _EFFECTS[action],
        )
        scope = (str(target.key), container_id, action.value)
        confirmation, expires_at = self._confirmations.issue(actor, scope)
        return ActionPlan(action, info, effects, confirmation, expires_at)

    def confirm(
        self,
        slug: str,
        env: EnvironmentName,
        container_id: str,
        confirmation: str,
        actor: Actor,
    ) -> ActionResult:
        """Execute the confirmed action on exactly the pinned container."""
        target = self._resolver.resolve(slug, env)
        scope = self._confirmations.consume(confirmation, actor, (str(target.key), container_id))
        action = LifecycleAction(scope[2])
        with self._locks.hold(str(target.key), timeout=self._config.lock_timeout):
            before = parse_container(self._membership.require(target, container_id))
            self._check_applicable(before, action)
            return self._execute(target, actor, action, before)

    def _execute(
        self,
        target: EnvironmentTarget,
        actor: Actor,
        action: LifecycleAction,
        before: ContainerInfo,
    ) -> ActionResult:
        details = {
            "action": action.value,
            "service": before.service,
            "name": before.name,
            "previous_state": before.state,
        }
        try:
            getattr(self._engine, action.value)(before.id)
        except ContainerRuntimeError as error:
            self._auditor.record(
                actor,
                f"container.{action.value}",
                target,
                before.id,
                {**details, "outcome": "failed"},
            )
            raise ConflictError(str(error)) from error
        after = parse_container(self._membership.require(target, before.id))
        details["outcome"] = "done"
        details["state"] = after.state
        self._auditor.record(actor, f"container.{action.value}", target, before.id, details)
        return ActionResult(action, before.state, after)

    @staticmethod
    def _check_applicable(info: ContainerInfo, action: LifecycleAction) -> None:
        if action is LifecycleAction.START and info.running:
            raise ConflictError("the container is already running")
        if action is LifecycleAction.STOP and not info.running:
            raise ConflictError("the container is not running")
        if action is LifecycleAction.RESTART and info.state == "removing":
            raise ValidationError("the container is being removed")
