"""Verifying that deployed services are up: container health, restarts and an optional URL."""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass

from shiftcodes.adapters.compose_project import ComposeProject
from shiftcodes.adapters.container_runtime import LineSink
from shiftcodes.adapters.container_state import ContainerInspector, ContainerState
from shiftcodes.adapters.deployer import HttpCheck
from shiftcodes.adapters.errors import AdapterError
from shiftcodes.adapters.http_probe import HttpProbe

HTTP_ATTEMPT_TIMEOUT = 10.0


class UnhealthyError(Exception):
    """A deployed service did not become healthy; the message says which and why."""


@dataclass(frozen=True, slots=True)
class HealthPolicy:
    """How long to wait and how steady a service must be."""

    timeout: float = 120.0
    settle: float = 3.0
    interval: float = 1.0


class Clock:
    """Time source the tests replace to run instantly."""

    def now(self) -> float:
        """Monotonic seconds."""
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        """Wait."""
        time.sleep(seconds)


def wait_for_status(
    probe: HttpProbe,
    check: HttpCheck,
    interval: float,
    sink: LineSink,
    clock: Clock | None = None,
) -> None:
    """GET ``check.url`` until the expected status; ``UnhealthyError`` when time is up."""
    time_source = clock or Clock()
    limit = time_source.now() + check.timeout
    last = "no attempt was made"
    while True:
        budget = min(HTTP_ATTEMPT_TIMEOUT, max(1.0, limit - time_source.now()))
        try:
            status = probe.get_status(check.url, budget)
        except AdapterError as error:
            last = str(error)
        else:
            if status == check.expect_status:
                sink(f"health check passed: {status}")
                return
            last = f"got status {status}, expected {check.expect_status}"
        sink(last)
        if time_source.now() >= limit:
            raise UnhealthyError(f"health check failed: {last}")
        time_source.sleep(min(interval, max(0.0, limit - time_source.now())))


class HealthVerifier:
    """Waits until every container of the given services is running and, when it has a
    healthcheck, healthy; fails when one exits, turns unhealthy or restarts."""

    def __init__(
        self,
        inspector: ContainerInspector,
        policy: HealthPolicy,
        probe: HttpProbe,
        clock: Clock | None = None,
    ) -> None:
        self._inspector = inspector
        self._policy = policy
        self._probe = probe
        self._clock = clock or Clock()

    def verify(
        self,
        project: ComposeProject,
        services: Sequence[str],
        sink: LineSink,
        http: HttpCheck | None = None,
    ) -> None:
        """Raise ``UnhealthyError`` unless all services are steady within the timeout."""
        self._containers(project, services, sink)
        if http is not None:
            wait_for_status(self._probe, http, self._policy.interval, sink, self._clock)

    def _containers(self, project: ComposeProject, services: Sequence[str], sink: LineSink) -> None:
        deadline = self._clock.now() + self._policy.timeout
        baseline: dict[str, int] = {}
        steady_since: float | None = None
        last = "no container was found"
        while True:
            problem = self._problem(project, services, baseline)
            now = self._clock.now()
            if problem is None:
                steady_since = now if steady_since is None else steady_since
                if now - steady_since >= self._policy.settle:
                    sink(f"healthy: {', '.join(services)}")
                    return
            else:
                steady_since = None
                last = problem
            if now >= deadline:
                raise UnhealthyError(f"not healthy after {self._policy.timeout:.0f}s: {last}")
            self._clock.sleep(self._policy.interval)

    def _problem(
        self, project: ComposeProject, services: Sequence[str], baseline: dict[str, int]
    ) -> str | None:
        """``None`` when every service is up now; a description when it is not yet. Failures
        that cannot recover raise ``UnhealthyError`` at once."""
        for service in services:
            states = self._inspector.containers(project.container_ids(service))
            if not states:
                return f"{service}: no container"
            for state in states:
                problem = self._judge(service, state, baseline)
                if problem is not None:
                    return problem
        return None

    @staticmethod
    def _judge(service: str, state: ContainerState, baseline: dict[str, int]) -> str | None:
        seen = baseline.setdefault(state.id, state.restart_count)
        if state.restart_count > seen:
            raise UnhealthyError(f"{service} restarted ({seen} -> {state.restart_count})")
        if state.status in {"exited", "dead"}:
            raise UnhealthyError(f"{service} exited with code {state.exit_code}")
        if state.status == "restarting":
            raise UnhealthyError(f"{service} is restarting repeatedly")
        if state.health == "unhealthy":
            raise UnhealthyError(f"{service} is unhealthy")
        if not state.running:
            return f"{service} is {state.status}"
        if state.health not in {None, "healthy"}:
            return f"{service} is {state.health}"
        return None
