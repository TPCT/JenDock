"""Runs one inspector operation inside a short-lived helper container.

Why a helper container and not a connection from the worker itself: the worker lives on its
own network, and the environment's database is only reachable on the environment's compose
network. Joining the worker to that network would give a long-lived, all-powerful process
standing access to every service in it. The helper instead

* is created per operation and removed when it exits (``--rm``), attached only to the
  environment's network, with a read-only root filesystem, no capabilities, an unprivileged
  user, memory, CPU and process limits and a hard wall-clock timeout;
* runs the small inspector image (drivers only; no Docker CLI, no key file, no platform data);
* receives the request, including the database password, through a 0600 env file that is
  deleted as soon as the container exists, never through argv;
* refuses to connect to anything outside the network's subnets or to its gateway (the host).
"""

from __future__ import annotations

import json
import logging
import secrets
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from shiftcodes.adapters.container_runtime import ExecResult, LineSink, RunSpec
from shiftcodes.adapters.errors import ContainerRuntimeError
from shiftcodes.domain.errors import ConfigurationError, ConflictError
from shiftcodes.inspector import wire
from shiftcodes.inspector.helper import REQUEST_ENV
from shiftcodes.inspector.model import ConnectionSpec, ErrorCode, InspectorError
from shiftcodes.inspector.netguard import NetworkPolicy

logger = logging.getLogger(__name__)
HELPER_MEMORY = "256m"
HELPER_CPUS = 0.5
HELPER_USER = "65534:65534"
HELPER_WALL_CLOCK = 30.0
HELPER_LABEL = "shiftcodes.inspector"
NAME_PREFIX = "shiftcodes-inspect-"


class HelperRuntime(Protocol):
    """The part of the docker runtime the runner needs (``DockerCliRuntime`` implements it)."""

    def query(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        """Run a short docker command."""

    def image_exists(self, image: str) -> bool:
        """Whether the image is present locally."""

    def run_container(
        self, spec: RunSpec, sink: LineSink, *, timeout: float | None = None
    ) -> ExecResult:
        """Run a container to completion."""


@dataclass(frozen=True, slots=True)
class HelperRequest:
    """One operation for the helper."""

    operation: str
    args: dict[str, object]
    connection: ConnectionSpec


class InspectorRunner:
    """Starts helper containers on an environment network."""

    def __init__(
        self,
        runtime: HelperRuntime,
        image: str,
        wall_clock: float = HELPER_WALL_CLOCK,
    ) -> None:
        self._runtime = runtime
        self._image = image
        self._wall_clock = wall_clock

    def run(self, network: str, request: HelperRequest) -> dict[str, object]:
        """Run ``request`` on ``network``; return the operation's result or raise
        ``InspectorError`` with the helper's failure."""
        if not self._runtime.image_exists(self._image):
            raise ConfigurationError(
                f"inspector image {self._image} is not available; build it first "
                "(docker compose --profile tools build inspector)"
            )
        policy = self._network_policy(network)
        spec = self._spec(network, request, policy)
        lines: list[str] = []
        try:
            result = self._runtime.run_container(spec, lines.append, timeout=self._wall_clock)
        except ContainerRuntimeError as error:
            raise InspectorError(
                ErrorCode.HELPER_FAILED, "the inspector helper could not run"
            ) from error
        return self._outcome(result, lines)

    def _spec(self, network: str, request: HelperRequest, policy: NetworkPolicy) -> RunSpec:
        payload = {
            "op": request.operation,
            "args": request.args,
            "connection": _connection_payload(request.connection),
            "network": {"subnets": list(policy.subnets), "blocked": list(policy.blocked)},
        }
        return RunSpec(
            image=self._image,
            name=NAME_PREFIX + secrets.token_hex(6),
            command=("python", "-m", "shiftcodes.inspector.helper"),
            env={REQUEST_ENV: json.dumps(payload, separators=(",", ":"))},
            network=network,
            labels={HELPER_LABEL: "1"},
            cpus=HELPER_CPUS,
            memory=HELPER_MEMORY,
            user=HELPER_USER,
            hardened=True,
        )

    def _outcome(self, result: ExecResult, lines: Sequence[str]) -> dict[str, object]:
        if result.timed_out:
            raise InspectorError(ErrorCode.TIMEOUT, "the inspector helper timed out")
        answer = wire.decode(lines)
        if answer is None or result.exit_code != 0:
            logger.warning(
                "inspector helper failed",
                extra={"exit_code": result.exit_code, "output_lines": len(lines)},
            )
            raise InspectorError(ErrorCode.HELPER_FAILED, "the inspector helper failed")
        return _unpack(answer)

    def _network_policy(self, network: str) -> NetworkPolicy:
        found = self._runtime.query(
            ["network", "inspect", "--format", "{{json .IPAM.Config}}", network]
        )
        if found.returncode != 0:
            raise ConflictError(
                f"environment network {network} does not exist; deploy the environment first"
            )
        try:
            configs = json.loads(found.stdout or "null") or []
        except ValueError as error:
            raise ConfigurationError("docker returned an unreadable network description") from error
        subnets = tuple(
            str(c["Subnet"]) for c in configs if isinstance(c, dict) and c.get("Subnet")
        )
        gateways = tuple(
            str(c["Gateway"]) for c in configs if isinstance(c, dict) and c.get("Gateway")
        )
        return NetworkPolicy(subnets, gateways)


def _connection_payload(spec: ConnectionSpec) -> dict[str, object]:
    return {
        "engine": spec.engine.value,
        "host": spec.host,
        "port": spec.port,
        "database": spec.database,
        "username": spec.username,
        "password": spec.password,
        "tls": spec.tls.value,
    }


def _unpack(answer: dict[str, object]) -> dict[str, object]:
    result = answer.get("result")
    if answer.get("ok") is True and isinstance(result, dict):
        return result
    error = answer.get("error")
    if isinstance(error, dict):
        try:
            code = ErrorCode(str(error.get("code")))
        except ValueError:
            code = ErrorCode.HELPER_FAILED
        raise InspectorError(code, str(error.get("message", "inspector failed"))[:500])
    raise InspectorError(ErrorCode.HELPER_FAILED, "the inspector helper failed")
