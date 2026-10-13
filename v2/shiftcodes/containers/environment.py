"""Environment variable editor: masked listing, audited reveal and managed overrides."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, replace

from shiftcodes.adapters.compose_overrides import (
    ComposeOverrideStore,
    OverrideFileError,
    OverrideSet,
)
from shiftcodes.adapters.docker_inspect import parse_container, parse_env
from shiftcodes.containers.base import (
    ContainerAuditor,
    EnvironmentTarget,
    Membership,
    TargetResolver,
)
from shiftcodes.containers.config import ContainerConfig
from shiftcodes.containers.locks import EnvironmentLocks
from shiftcodes.containers.recreate import RecreateFailedError, ServiceRecreator
from shiftcodes.domain.actor import Actor
from shiftcodes.domain.containers import (
    ContainerInfo,
    EnvChange,
    EnvVarInfo,
    is_sensitive_name,
    validate_env_change,
    validate_env_name,
)
from shiftcodes.domain.enums import EnvironmentName
from shiftcodes.domain.errors import ConflictError, NotFoundError, ValidationError

logger = logging.getLogger(__name__)
MAX_MESSAGE_CHARS = 300


@dataclass(frozen=True, slots=True)
class EnvUpdateResult:
    """The service's containers after an edit; ``restarted`` is false for a stopped service."""

    service: str
    containers: tuple[ContainerInfo, ...]
    restarted: bool


class EnvironmentEditor:
    """Lists variables with masked values, reveals one on request, edits through overrides."""

    def __init__(  # noqa: PLR0913, PLR0917 - collaborators of one use case
        self,
        resolver: TargetResolver,
        membership: Membership,
        store: ComposeOverrideStore,
        recreator: ServiceRecreator,
        locks: EnvironmentLocks,
        auditor: ContainerAuditor,
        config: ContainerConfig,
    ) -> None:
        self._resolver = resolver
        self._membership = membership
        self._store = store
        self._recreator = recreator
        self._locks = locks
        self._auditor = auditor
        self._config = config

    def list(self, slug: str, env: EnvironmentName, container_id: str) -> list[EnvVarInfo]:
        """Names only; ``overridden`` marks variables managed by the platform."""
        target = self._resolver.resolve(slug, env)
        record, service = self._membership.require_service(target, container_id)
        overridden = self._load(target).for_service(service).env
        return [
            EnvVarInfo(name, is_sensitive_name(name), name in overridden)
            for name in sorted(parse_env(record))
        ]

    def reveal(
        self, slug: str, env: EnvironmentName, container_id: str, name: str, actor: Actor
    ) -> str:
        """The value of one variable of the running configuration; the access is audited."""
        validate_env_name(name)
        target = self._resolver.resolve(slug, env)
        record, service = self._membership.require_service(target, container_id)
        environment = parse_env(record)
        if name not in environment:
            raise NotFoundError("variable not found")
        self._auditor.record(
            actor,
            "container.env.reveal",
            target,
            container_id,
            {"service": service, "name": name},
        )
        return environment[name]

    def update(
        self,
        slug: str,
        env: EnvironmentName,
        container_id: str,
        change: EnvChange,
        actor: Actor,
    ) -> EnvUpdateResult:
        """Store the edit as the service's override and recreate only that service.

        A running service is recreated and verified (running, healthy when it has a health
        check, stable for a few seconds); a stopped service is only re-created and stays
        stopped. Any failure restores the previous override file and recreates the service
        again with it.
        """
        validated = validate_env_change(change)
        target = self._resolver.resolve(slug, env)
        self._recreator.require_compose_files(target)
        with self._locks.hold(str(target.key), timeout=self._config.lock_timeout):
            record, service = self._membership.require_service(target, container_id)
            was_running = parse_container(record).running
            return self._apply(target, service, container_id, validated, was_running, actor)

    def _apply(  # noqa: PLR0913, PLR0917 - one operation's context
        self,
        target: EnvironmentTarget,
        service: str,
        container_id: str,
        change: EnvChange,
        was_running: bool,
        actor: Actor,
    ) -> EnvUpdateResult:
        current = self._load(target)
        existing = current.for_service(service)
        merged = {**existing.env, **change.set}
        for name in change.remove:
            merged.pop(name, None)
        updated = current.with_service(service, replace(existing, env=merged))
        if updated == current:
            raise ValidationError("this change does not alter the overrides")
        snapshot = self._store.snapshot(target.key)
        details: dict[str, object] = {
            "service": service,
            "names_set": sorted(change.set),
            "names_removed": sorted(change.remove),
            "was_running": was_running,
        }
        try:
            self._store.save(target.key, updated)
            outcome = self._recreator.recreate(target, service, start=was_running)
        except (RecreateFailedError, OverrideFileError) as error:
            restored = self._rollback(target, service, snapshot, was_running)
            self._auditor.record(
                actor,
                "container.env.update",
                target,
                container_id,
                {**details, "outcome": "restored" if restored else "restore_failed"},
            )
            message = _failure_message(error, restored=restored, values=change.set.values())
            raise ConflictError(message) from error
        self._auditor.record(
            actor, "container.env.update", target, container_id, {**details, "outcome": "applied"}
        )
        return EnvUpdateResult(service, outcome.containers, restarted=was_running)

    def _rollback(
        self, target: EnvironmentTarget, service: str, snapshot: bytes | None, was_running: bool
    ) -> bool:
        """Put the previous file back and recreate the service with it; report success."""
        try:
            self._store.restore(target.key, snapshot)
        except OverrideFileError:
            logger.exception("cannot restore the override file", extra={"service": service})
            return False
        try:
            self._recreator.recreate(target, service, start=was_running)
        except RecreateFailedError:
            logger.exception("service did not recover after rollback", extra={"service": service})
            return False
        return True

    def _load(self, target: EnvironmentTarget) -> OverrideSet:
        try:
            return self._store.load(target.key)
        except OverrideFileError as error:
            raise ConflictError(str(error)) from error


def _failure_message(error: Exception, *, restored: bool, values: Iterable[str]) -> str:
    reason = str(error)
    for value in sorted(values, key=len, reverse=True):
        if value:
            reason = reason.replace(value, "[REDACTED]")
    reason = reason[:MAX_MESSAGE_CHARS]
    if restored:
        return f"the update failed ({reason}); the previous configuration was restored"
    return f"the update failed ({reason}) and the previous configuration could not be restored"
