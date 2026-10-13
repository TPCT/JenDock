"""Opening a terminal in a managed container, and auditing its life (metadata only)."""

from __future__ import annotations

import secrets

from shiftcodes.adapters.docker_client import DockerClient
from shiftcodes.adapters.docker_inspect import parse_container
from shiftcodes.containers.base import ContainerAuditor, Membership, TargetResolver
from shiftcodes.domain.errors import ConflictError
from shiftcodes.terminal.server import OpenRequest
from shiftcodes.terminal.session import OpenSpec, TerminalSession

SHELL = "/bin/sh"
MARKER_VARIABLE = "SHIFTCODES_TERMINAL"
# Ends every process of the container whose environment carries the marker given as $1. The
# marker is passed as an argument, never interpolated into this script.
KILL_SCRIPT = (
    'for d in /proc/[0-9]*; do pid="${d#/proc/}"; '
    'if tr "\\0" "\\n" < "$d/environ" 2>/dev/null | grep -qx "'
    + MARKER_VARIABLE
    + '=$1"; then kill -HUP "$pid" 2>/dev/null; kill -KILL "$pid" 2>/dev/null; fi; done'
)
TERM_VALUE = "xterm-256color"


class DockerExecOpener:
    """``docker exec -it <exact container id> /bin/sh`` for a container of the environment."""

    def __init__(
        self, resolver: TargetResolver, membership: Membership, client: DockerClient
    ) -> None:
        self._resolver = resolver
        self._membership = membership
        self._client = client

    def prepare(self, request: OpenRequest) -> OpenSpec:
        """Check the container belongs to the environment and runs; pin its id in the argv."""
        target = self._resolver.resolve(request.slug, request.env)
        info = parse_container(self._membership.require(target, request.container_id))
        if not info.running:
            raise ConflictError("start the container to open a terminal")
        marker = secrets.token_urlsafe(12)
        argv = [
            self._client.binary,
            "exec",
            "--interactive",
            "--tty",
            "--env",
            f"TERM={TERM_VALUE}",
            "--env",
            f"{MARKER_VARIABLE}={marker}",
            info.id,
            SHELL,
        ]
        return OpenSpec(
            argv=argv,
            env=dict(self._client.env),
            rows=request.rows,
            cols=request.cols,
            actor=request.actor.to_actor(),
            target=target,
            container_id=info.id,
            container_name=info.name,
            service=info.service,
            cleanup=lambda: self._kill_shell(info.id, marker),
        )

    def _kill_shell(self, container_id: str, marker: str) -> None:
        """Stop the shell the dead ``docker exec`` client left behind in the container."""
        self._client.run(
            ["exec", container_id, "/bin/sh", "-c", KILL_SCRIPT, "sh", marker], timeout=15.0
        )


class AuditTerminalListener:
    """Records who opened which terminal and for how long. Never commands or output."""

    def __init__(self, auditor: ContainerAuditor) -> None:
        self._auditor = auditor

    def opened(self, session: TerminalSession) -> None:
        """Audit ``terminal.open``."""
        spec = session.spec
        self._auditor.record(
            spec.actor,
            "terminal.open",
            spec.target,
            spec.container_id,
            {"terminal_id": session.id, "service": spec.service, "name": spec.container_name},
        )

    def closed(
        self, session: TerminalSession, reason: str, duration: float, code: int | None
    ) -> None:
        """Audit ``terminal.close`` with the reason and the duration."""
        spec = session.spec
        self._auditor.record(
            spec.actor,
            "terminal.close",
            spec.target,
            spec.container_id,
            {
                "terminal_id": session.id,
                "reason": reason,
                "duration_seconds": round(duration, 1),
                "exit_code": code,
            },
        )
