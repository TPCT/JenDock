"""Recreating one service and verifying that it came back (or that it stayed stopped)."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from shiftcodes.adapters.compose_cli import ComposeEngine, ComposeTarget
from shiftcodes.adapters.compose_overrides import ComposeOverrideStore
from shiftcodes.adapters.container_admin import ContainerEngine
from shiftcodes.adapters.docker_inspect import parse_container, service_of
from shiftcodes.adapters.errors import AdapterError
from shiftcodes.adapters.override_store import OverrideStore
from shiftcodes.containers.base import EnvironmentTarget
from shiftcodes.containers.config import ContainerConfig
from shiftcodes.domain.containers import ContainerInfo
from shiftcodes.domain.errors import ValidationError


class RecreateFailedError(Exception):
    """The service could not be recreated or did not become healthy."""


class OverrideFiles(Protocol):
    """The managed override files of an environment, in the order compose must read them."""

    def paths(self, target: EnvironmentTarget) -> list[str]:
        """Existing override files (this module's and any other writer's)."""


class StoreOverrideFiles:
    """Only the container-management override file."""

    def __init__(self, store: ComposeOverrideStore) -> None:
        self._store = store

    def paths(self, target: EnvironmentTarget) -> list[str]:
        """The file of ``ComposeOverrideStore`` when it exists."""
        args = self._store.compose_file_args(target.key)
        return args[1::2]


class CombinedOverrideFiles:
    """Several writers' override files; compose reads them in the order given."""

    def __init__(self, *parts: OverrideFiles) -> None:
        self._parts = parts

    def paths(self, target: EnvironmentTarget) -> list[str]:
        """Every part's files, in order."""
        return [path for part in self._parts for path in part.paths(target)]


class DeployOverrideFiles:
    """The deployer's managed image override (``OverrideStore``): it must be the last file."""

    def __init__(self, store: OverrideStore) -> None:
        self._store = store

    def paths(self, target: EnvironmentTarget) -> list[str]:
        """The image override of the environment's compose project, once something was deployed."""
        path = self._store.file_for_compose(target.compose_project)
        return [str(path)] if path is not None else []


@dataclass(frozen=True, slots=True)
class RecreateOutcome:
    """The containers of the service after a successful recreate."""

    containers: tuple[ContainerInfo, ...]


class ServiceRecreator:
    """``up -d --no-deps --no-build`` for one service plus a verification of the result."""

    def __init__(  # noqa: PLR0913, PLR0917 - collaborators of one use case
        self,
        engine: ContainerEngine,
        compose: ComposeEngine,
        files: OverrideFiles,
        config: ContainerConfig,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._engine = engine
        self._compose = compose
        self._files = files
        self._config = config
        self._sleep = sleep
        self._monotonic = monotonic

    def require_compose_files(self, target: EnvironmentTarget) -> None:
        """The environment must name the compose files that define its services."""
        if not target.compose_files:
            raise ValidationError("this environment has no compose files configured")

    def recreate(self, target: EnvironmentTarget, service: str, *, start: bool) -> RecreateOutcome:
        """Recreate ``service`` with the current files and verify the result."""
        files = (*target.compose_files, *self._files.paths(target))
        try:
            self._compose.recreate(
                ComposeTarget(target.compose_project, files, target.compose_env_files),
                service,
                start=start,
            )
        except AdapterError as error:
            raise RecreateFailedError(str(error)) from error
        return RecreateOutcome(self._verify(target, service, expect_running=start))

    def _service_records(
        self, target: EnvironmentTarget, service: str
    ) -> list[Mapping[str, object]]:
        try:
            records = self._engine.list_project(target.compose_project)
        except AdapterError as error:
            raise RecreateFailedError(str(error)) from error
        return [record for record in records if service_of(record) == service]

    def _verify(
        self, target: EnvironmentTarget, service: str, *, expect_running: bool
    ) -> tuple[ContainerInfo, ...]:
        deadline = self._monotonic() + self._config.verify_timeout
        stable_since: dict[str, tuple[float, tuple[str, int]]] = {}
        while True:
            infos = tuple(parse_container(r) for r in self._service_records(target, service))
            if not infos:
                raise RecreateFailedError("the service has no container after the update")
            verdict = self._judge(infos, expect_running, stable_since)
            if verdict is True:
                return infos
            if isinstance(verdict, str):
                raise RecreateFailedError(verdict)
            if self._monotonic() >= deadline:
                raise RecreateFailedError("the service did not become ready in time")
            self._sleep(self._config.poll_seconds)

    def _judge(
        self,
        infos: tuple[ContainerInfo, ...],
        expect_running: bool,
        stable_since: dict[str, tuple[float, tuple[str, int]]],
    ) -> bool | str:
        """True: verified; a string: definitely failed; False: keep waiting."""
        if not expect_running:
            running = [info for info in infos if info.running]
            return f"{running[0].name} is running although it was stopped" if running else True
        now = self._monotonic()
        ready = True
        for info in infos:
            if info.health == "unhealthy":
                return f"{info.name} is unhealthy"
            if info.state in {"exited", "dead"}:
                return f"{info.name} exited with code {info.exit_code}"
            if not info.running or info.health == "starting":
                ready = False
                continue
            identity = (info.started_at.isoformat() if info.started_at else "", info.restart_count)
            seen = stable_since.get(info.id)
            if seen is None or seen[1] != identity:
                stable_since[info.id] = (now, identity)
                seen = stable_since[info.id]
            if info.health != "healthy" and now - seen[0] < self._config.settle_seconds:
                ready = False
        return ready
