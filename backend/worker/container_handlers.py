"""RPC methods of container management (``containers.*`` and ``telemetry.host``)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from backend.containers.environment import EnvironmentEditor
from backend.containers.files import DEFAULT_READ_BYTES, FileBrowser
from backend.containers.lifecycle import ContainerInventory, LifecycleService
from backend.containers.limits import LimitsService
from backend.containers.logs import DEFAULT_TAIL, LogCursor, LogService
from backend.containers.telemetry import TelemetryService
from backend.containers.wire import (
    ActionPlanView,
    ActionResultView,
    ContainerSampleView,
    ContainerStatsView,
    ContainerView,
    DirectoryView,
    EnvRevealView,
    EnvUpdateView,
    EnvVarView,
    FileView,
    HostSampleView,
    HostTelemetryView,
    LogPageView,
)
from backend.domain.containers import EnvChange, LifecycleAction, LimitChange
from backend.domain.enums import EnvironmentName
from backend.worker.rpc import BadRequestError, Handler, RpcCall

CONTAINER_ID = r"^[a-f0-9]{64}$"
MAX_TIMESTAMP = 40


@dataclass(frozen=True, slots=True)
class ContainerServices:
    """Every container-management use case the worker serves."""

    inventory: ContainerInventory
    lifecycle: LifecycleService
    logs: LogService
    env: EnvironmentEditor
    limits: LimitsService
    files: FileBrowser
    telemetry: TelemetryService


class _Params(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class _EnvParams(_Params):
    slug: str = Field(max_length=64)
    env: EnvironmentName


class _ContainerParams(_EnvParams):
    container_id: str = Field(pattern=CONTAINER_ID)


class _PlanParams(_ContainerParams):
    action: LifecycleAction


class _ConfirmParams(_ContainerParams):
    confirmation: str = Field(min_length=1, max_length=200)


class _LogParams(_ContainerParams):
    tail: int = Field(default=DEFAULT_TAIL, ge=1, le=10_000)
    since: str | None = Field(default=None, max_length=MAX_TIMESTAMP)
    skip: int = Field(default=0, ge=0, le=100_000)


class _NameParams(_ContainerParams):
    name: str = Field(max_length=128)


class _EnvUpdateParams(_ContainerParams):
    set: dict[str, str] = Field(default_factory=dict)
    remove: list[str] = Field(default_factory=list)


class _LimitParams(_ContainerParams):
    cpus: float | None = None
    memory: str | None = Field(default=None, max_length=32)


class _PathParams(_ContainerParams):
    path: str = Field(max_length=1100)


class _ReadParams(_PathParams):
    max_bytes: int = Field(default=DEFAULT_READ_BYTES, ge=1, le=10_000_000)


def _parse[P: _Params](model: type[P], call: RpcCall) -> P:
    try:
        return model.model_validate(call.params, strict=False)
    except PydanticValidationError as error:
        # Never echo the input: environment values travel in some of these calls.
        raise BadRequestError("invalid params") from error


def _dump(view: BaseModel) -> dict[str, object]:
    return view.model_dump(mode="json")


class ContainerHandlers:
    """Adapts the container services to RPC calls."""

    def __init__(self, services: ContainerServices) -> None:
        self._s = services

    def list(self, call: RpcCall) -> object:
        """``containers.list``."""
        params = _parse(_EnvParams, call)
        call.require_actor()
        infos = self._s.inventory.list(params.slug, params.env)
        return [_dump(ContainerView.of(info)) for info in infos]

    def get(self, call: RpcCall) -> object:
        """``containers.get``."""
        params = _parse(_ContainerParams, call)
        call.require_actor()
        return _dump(
            ContainerView.of(self._s.inventory.get(params.slug, params.env, params.container_id))
        )

    def plan(self, call: RpcCall) -> object:
        """``containers.action.plan``."""
        params = _parse(_PlanParams, call)
        plan = self._s.lifecycle.plan(
            params.slug, params.env, params.container_id, params.action, call.require_actor()
        )
        return _dump(ActionPlanView.of(plan))

    def confirm(self, call: RpcCall) -> object:
        """``containers.action.confirm``."""
        params = _parse(_ConfirmParams, call)
        result = self._s.lifecycle.confirm(
            params.slug, params.env, params.container_id, params.confirmation, call.require_actor()
        )
        return _dump(ActionResultView.of(result))

    def logs(self, call: RpcCall) -> object:
        """``containers.logs``: the tail, or the lines after ``since``/``skip``."""
        params = _parse(_LogParams, call)
        call.require_actor()
        cursor = LogCursor(params.since, params.skip) if params.since else None
        page = self._s.logs.read(
            params.slug, params.env, params.container_id, tail=params.tail, cursor=cursor
        )
        return _dump(LogPageView.of(page))

    def env_list(self, call: RpcCall) -> object:
        """``containers.env.list``: names only."""
        params = _parse(_ContainerParams, call)
        call.require_actor()
        items = self._s.env.list(params.slug, params.env, params.container_id)
        return [_dump(EnvVarView.of(item)) for item in items]

    def env_reveal(self, call: RpcCall) -> object:
        """``containers.env.reveal``: one value, audited."""
        params = _parse(_NameParams, call)
        value = self._s.env.reveal(
            params.slug, params.env, params.container_id, params.name, call.require_actor()
        )
        return _dump(EnvRevealView(name=params.name, value=value))

    def env_update(self, call: RpcCall) -> object:
        """``containers.env.update``: set/remove overrides and recreate the service."""
        params = _parse(_EnvUpdateParams, call)
        change = EnvChange(set=params.set, remove=tuple(params.remove))
        result = self._s.env.update(
            params.slug, params.env, params.container_id, change, call.require_actor()
        )
        return _dump(EnvUpdateView.of(result))

    def limits(self, call: RpcCall) -> object:
        """``containers.limits.update``."""
        params = _parse(_LimitParams, call)
        info = self._s.limits.update(
            params.slug,
            params.env,
            params.container_id,
            LimitChange(params.cpus, params.memory),
            call.require_actor(),
        )
        return _dump(ContainerView.of(info))

    def files_list(self, call: RpcCall) -> object:
        """``containers.files.list``."""
        params = _parse(_PathParams, call)
        call.require_actor()
        listing = self._s.files.list(params.slug, params.env, params.container_id, params.path)
        return _dump(DirectoryView.of(listing))

    def files_read(self, call: RpcCall) -> object:
        """``containers.files.read``."""
        params = _parse(_ReadParams, call)
        content = self._s.files.read(
            params.slug,
            params.env,
            params.container_id,
            params.path,
            call.require_actor(),
            max_bytes=params.max_bytes,
        )
        return _dump(FileView.of(content))

    def stats(self, call: RpcCall) -> object:
        """``containers.stats``."""
        params = _parse(_ContainerParams, call)
        call.require_actor()
        latest, history = self._s.telemetry.container(params.slug, params.env, params.container_id)
        return _dump(
            ContainerStatsView(
                latest=ContainerSampleView.of(latest),
                history=tuple(ContainerSampleView.of(item) for item in history),
            )
        )

    def host(self, call: RpcCall) -> object:
        """``telemetry.host``."""
        call.require_actor()
        latest, history = self._s.telemetry.host()
        return _dump(
            HostTelemetryView(
                latest=HostSampleView.of(latest),
                history=tuple(HostSampleView.of(item) for item in history),
            )
        )


def build_container_handlers(services: ContainerServices) -> Mapping[str, Handler]:
    """The allowlist of container methods."""
    handlers = ContainerHandlers(services)
    return {
        "containers.list": handlers.list,
        "containers.get": handlers.get,
        "containers.action.plan": handlers.plan,
        "containers.action.confirm": handlers.confirm,
        "containers.logs": handlers.logs,
        "containers.env.list": handlers.env_list,
        "containers.env.reveal": handlers.env_reveal,
        "containers.env.update": handlers.env_update,
        "containers.limits.update": handlers.limits,
        "containers.files.list": handlers.files_list,
        "containers.files.read": handlers.files_read,
        "containers.stats": handlers.stats,
        "telemetry.host": handlers.host,
    }
