"""The API's view of container management: typed calls over the worker socket."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel
from pydantic import ValidationError as PydanticValidationError

from shiftcodes.adapters.worker_client import WorkerClient
from shiftcodes.containers.logs import LogCursor
from shiftcodes.containers.wire import (
    ActionPlanView,
    ActionResultView,
    ContainerStatsView,
    ContainerView,
    DirectoryView,
    EnvRevealView,
    EnvUpdateView,
    EnvVarView,
    FileView,
    HostTelemetryView,
    LogPageView,
)
from shiftcodes.domain.actor import Actor
from shiftcodes.domain.containers import LifecycleAction
from shiftcodes.domain.enums import EnvironmentName
from shiftcodes.domain.errors import WorkerError

QUICK_TIMEOUT = 20.0
STATS_TIMEOUT = 90.0
ACTION_TIMEOUT = 150.0
# compose up (180 s) + verification (60 s), twice when a rollback is needed.
ENV_UPDATE_TIMEOUT = 560.0


class ContainersGateway(Protocol):
    """Container management as the API needs it; ``ContainerClient`` and test fakes implement it."""

    def list_containers(
        self, slug: str, env: EnvironmentName, actor: Actor
    ) -> Sequence[ContainerView]:
        """Inventory of the environment."""

    def get_container(
        self, slug: str, env: EnvironmentName, container_id: str, actor: Actor
    ) -> ContainerView:
        """One container."""

    def plan_action(
        self,
        slug: str,
        env: EnvironmentName,
        container_id: str,
        action: LifecycleAction,
        actor: Actor,
    ) -> ActionPlanView:
        """Step one of start/stop/restart."""

    def confirm_action(
        self, slug: str, env: EnvironmentName, container_id: str, confirmation: str, actor: Actor
    ) -> ActionResultView:
        """Step two of start/stop/restart."""

    def logs(  # noqa: PLR0913 - one query
        self,
        slug: str,
        env: EnvironmentName,
        container_id: str,
        actor: Actor,
        *,
        tail: int,
        cursor: LogCursor | None,
    ) -> LogPageView:
        """Tail, or the lines after a cursor."""

    def env_list(
        self, slug: str, env: EnvironmentName, container_id: str, actor: Actor
    ) -> Sequence[EnvVarView]:
        """Variable names (values masked)."""

    def env_reveal(
        self, slug: str, env: EnvironmentName, container_id: str, name: str, actor: Actor
    ) -> EnvRevealView:
        """One value (audited by the worker)."""

    def env_update(  # noqa: PLR0913, PLR0917 - one command
        self,
        slug: str,
        env: EnvironmentName,
        container_id: str,
        values: dict[str, str],
        remove: Sequence[str],
        actor: Actor,
    ) -> EnvUpdateView:
        """Edit overrides and recreate the service."""

    def update_limits(  # noqa: PLR0913, PLR0917 - one command
        self,
        slug: str,
        env: EnvironmentName,
        container_id: str,
        cpus: float | None,
        memory: str | None,
        actor: Actor,
    ) -> ContainerView:
        """Set CPU/memory limits."""

    def list_files(
        self, slug: str, env: EnvironmentName, container_id: str, path: str, actor: Actor
    ) -> DirectoryView:
        """List a directory."""

    def read_file(  # noqa: PLR0913, PLR0917 - one query
        self,
        slug: str,
        env: EnvironmentName,
        container_id: str,
        path: str,
        max_bytes: int,
        actor: Actor,
    ) -> FileView:
        """Read a file."""

    def container_stats(
        self, slug: str, env: EnvironmentName, container_id: str, actor: Actor
    ) -> ContainerStatsView:
        """Latest stats and history."""

    def host_telemetry(self, actor: Actor) -> HostTelemetryView:
        """Host reading and history."""


def _env(slug: str, env: EnvironmentName) -> dict[str, object]:
    return {"slug": slug, "env": env.value}


def _target(slug: str, env: EnvironmentName, container_id: str) -> dict[str, object]:
    return {**_env(slug, env), "container_id": container_id}


def _view[V: BaseModel](model: type[V], value: object) -> V:
    try:
        return model.model_validate(value)
    except PydanticValidationError as error:
        raise WorkerError("worker returned an unexpected response") from error


class ContainerClient:
    """``ContainersGateway`` over the worker RPC socket."""

    def __init__(self, worker: WorkerClient) -> None:
        self._worker = worker

    def list_containers(
        self, slug: str, env: EnvironmentName, actor: Actor
    ) -> Sequence[ContainerView]:
        """``containers.list``."""
        result = self._worker.call("containers.list", actor, _env(slug, env), timeout=QUICK_TIMEOUT)
        return [_view(ContainerView, item) for item in _items(result)]

    def get_container(
        self, slug: str, env: EnvironmentName, container_id: str, actor: Actor
    ) -> ContainerView:
        """``containers.get``."""
        params = _target(slug, env, container_id)
        return _view(
            ContainerView, self._worker.call("containers.get", actor, params, timeout=QUICK_TIMEOUT)
        )

    def plan_action(
        self,
        slug: str,
        env: EnvironmentName,
        container_id: str,
        action: LifecycleAction,
        actor: Actor,
    ) -> ActionPlanView:
        """``containers.action.plan``."""
        params = {**_target(slug, env, container_id), "action": action.value}
        result = self._worker.call("containers.action.plan", actor, params, timeout=QUICK_TIMEOUT)
        return _view(ActionPlanView, result)

    def confirm_action(
        self, slug: str, env: EnvironmentName, container_id: str, confirmation: str, actor: Actor
    ) -> ActionResultView:
        """``containers.action.confirm``."""
        params = {**_target(slug, env, container_id), "confirmation": confirmation}
        result = self._worker.call(
            "containers.action.confirm", actor, params, timeout=ACTION_TIMEOUT
        )
        return _view(ActionResultView, result)

    def logs(  # noqa: PLR0913 - one query
        self,
        slug: str,
        env: EnvironmentName,
        container_id: str,
        actor: Actor,
        *,
        tail: int,
        cursor: LogCursor | None,
    ) -> LogPageView:
        """``containers.logs``."""
        params: dict[str, object] = {**_target(slug, env, container_id), "tail": tail}
        if cursor is not None:
            params["since"] = cursor.since
            params["skip"] = cursor.skip
        result = self._worker.call("containers.logs", actor, params, timeout=QUICK_TIMEOUT)
        return _view(LogPageView, result)

    def env_list(
        self, slug: str, env: EnvironmentName, container_id: str, actor: Actor
    ) -> Sequence[EnvVarView]:
        """``containers.env.list``."""
        params = _target(slug, env, container_id)
        result = self._worker.call("containers.env.list", actor, params, timeout=QUICK_TIMEOUT)
        return [_view(EnvVarView, item) for item in _items(result)]

    def env_reveal(
        self, slug: str, env: EnvironmentName, container_id: str, name: str, actor: Actor
    ) -> EnvRevealView:
        """``containers.env.reveal``."""
        params = {**_target(slug, env, container_id), "name": name}
        result = self._worker.call("containers.env.reveal", actor, params, timeout=QUICK_TIMEOUT)
        return _view(EnvRevealView, result)

    def env_update(  # noqa: PLR0913, PLR0917 - one command
        self,
        slug: str,
        env: EnvironmentName,
        container_id: str,
        values: dict[str, str],
        remove: Sequence[str],
        actor: Actor,
    ) -> EnvUpdateView:
        """``containers.env.update``."""
        params = {**_target(slug, env, container_id), "set": values, "remove": list(remove)}
        result = self._worker.call(
            "containers.env.update", actor, params, timeout=ENV_UPDATE_TIMEOUT
        )
        return _view(EnvUpdateView, result)

    def update_limits(  # noqa: PLR0913, PLR0917 - one command
        self,
        slug: str,
        env: EnvironmentName,
        container_id: str,
        cpus: float | None,
        memory: str | None,
        actor: Actor,
    ) -> ContainerView:
        """``containers.limits.update``."""
        params = {**_target(slug, env, container_id), "cpus": cpus, "memory": memory}
        result = self._worker.call(
            "containers.limits.update", actor, params, timeout=ACTION_TIMEOUT
        )
        return _view(ContainerView, result)

    def list_files(
        self, slug: str, env: EnvironmentName, container_id: str, path: str, actor: Actor
    ) -> DirectoryView:
        """``containers.files.list``."""
        params = {**_target(slug, env, container_id), "path": path}
        result = self._worker.call("containers.files.list", actor, params, timeout=QUICK_TIMEOUT)
        return _view(DirectoryView, result)

    def read_file(  # noqa: PLR0913, PLR0917 - one query
        self,
        slug: str,
        env: EnvironmentName,
        container_id: str,
        path: str,
        max_bytes: int,
        actor: Actor,
    ) -> FileView:
        """``containers.files.read``."""
        params = {**_target(slug, env, container_id), "path": path, "max_bytes": max_bytes}
        result = self._worker.call("containers.files.read", actor, params, timeout=QUICK_TIMEOUT)
        return _view(FileView, result)

    def container_stats(
        self, slug: str, env: EnvironmentName, container_id: str, actor: Actor
    ) -> ContainerStatsView:
        """``containers.stats``."""
        params = _target(slug, env, container_id)
        result = self._worker.call("containers.stats", actor, params, timeout=STATS_TIMEOUT)
        return _view(ContainerStatsView, result)

    def host_telemetry(self, actor: Actor) -> HostTelemetryView:
        """``telemetry.host``."""
        result = self._worker.call("telemetry.host", actor, {}, timeout=QUICK_TIMEOUT)
        return _view(HostTelemetryView, result)


def _items(result: object) -> list[object]:
    if not isinstance(result, list):
        raise WorkerError("worker returned an unexpected response")
    return result
