"""Opening a terminal in a managed container, and auditing its life (metadata only)."""

from __future__ import annotations

from shiftcodes.adapters.docker_client import DockerClient
from shiftcodes.adapters.docker_inspect import parse_container
from shiftcodes.containers.base import ContainerAuditor, Membership, TargetResolver
from shiftcodes.domain.errors import ConflictError
from shiftcodes.terminal.server import OpenRequest
from shiftcodes.terminal.session import OpenSpec, TerminalSession

SHELL = "/bin/sh"
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
        argv = [
            self._client.binary,
            "exec",
            "--interactive",
            "--tty",
            "--env",
            f"TERM={TERM_VALUE}",
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
