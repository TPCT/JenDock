"""Putting the previous images of a failed or interrupted deployment back."""

from __future__ import annotations

from dataclasses import dataclass

from backend.adapters.compose_project import ComposeProject
from backend.adapters.container_runtime import LineSink
from backend.adapters.container_state import ContainerInspector
from backend.adapters.deploy_health import HealthVerifier, UnhealthyError
from backend.adapters.deploy_journal import JournalEntry
from backend.adapters.docker_commands import CommandRunner
from backend.adapters.errors import AdapterError
from backend.adapters.override_store import OverrideStore


@dataclass(frozen=True, slots=True)
class RollbackResult:
    """Whether the previous state is running again, and what happened."""

    ok: bool
    message: str


class Rollback:
    """Restores the override that was in force before a deployment, recreates the selected
    services from their previous images and verifies them. A service that did not exist
    before the deployment is stopped and removed."""

    def __init__(
        self,
        docker: CommandRunner,
        overrides: OverrideStore,
        inspector: ContainerInspector,
        verifier: HealthVerifier,
        timeout: float,
    ) -> None:
        self._docker = docker
        self._overrides = overrides
        self._inspector = inspector
        self._verifier = verifier
        self._timeout = timeout

    def run(self, entry: JournalEntry, sink: LineSink) -> RollbackResult:
        """Roll back; never raises for the failures it reports."""
        try:
            self._restore(entry, sink)
        except (AdapterError, UnhealthyError) as error:
            return RollbackResult(False, str(error))
        return RollbackResult(True, "previous images are running again")

    def _restore(self, entry: JournalEntry, sink: LineSink) -> None:
        key = entry.compose_project
        sink("rolling back to the previous images")
        self._overrides.write(key, self._restored_override(entry))
        project = ComposeProject(
            self._docker,
            key,
            entry.compose_files,
            self._overrides.path(key),
            entry.compose_env_files,
        )
        for change in entry.changes:
            if change.previous_image_id is None:
                project.remove(change.service, sink, self._timeout)
        start = [c.service for c in entry.changes if c.previous_image_id is not None]
        if not start:
            return
        result = project.up(start, sink, self._timeout)
        if not result.ok:
            raise AdapterError(
                f"docker compose up of the previous images failed (exit code {result.exit_code})"
            )
        self._verifier.verify(project, start, sink)

    def _restored_override(self, entry: JournalEntry) -> dict[str, str]:
        restored = dict(entry.previous_override)
        for change in entry.changes:
            if change.previous_image_id is None:
                restored.pop(change.service, None)
                continue
            candidate = entry.previous_override.get(change.service) or change.previous_image
            if candidate and self._inspector.image_id(candidate) == change.previous_image_id:
                if change.service not in entry.previous_override:
                    restored.pop(change.service, None)
            else:
                # The tag moved or is gone: pin the exact image that was running.
                restored[change.service] = change.previous_image_id
        return restored
