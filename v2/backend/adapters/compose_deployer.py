"""Deployment with Docker Compose.

For the requested services of an environment the deployer pins each service's ``image`` to the
run's built tag in a managed override file (kept for future restarts), then runs
``docker compose up -d --no-deps --no-build`` for those services only. Before anything changes
it records the running images and writes a journal; when the new containers are not healthy it
puts the previous images back. See ``deploy_rollback`` and ``deploy_health``.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path

from backend.adapters.compose_project import ComposeProject, ensure_readable
from backend.adapters.container_runtime import CancelSignal, LineSink
from backend.adapters.container_state import ContainerInspector
from backend.adapters.deploy_health import Clock, HealthPolicy, HealthVerifier
from backend.adapters.deploy_journal import DeployJournal, JournalEntry
from backend.adapters.deploy_ledger import DeploymentLedger, ServiceChange
from backend.adapters.deploy_rollback import Rollback
from backend.adapters.deployer import DeployRequest
from backend.adapters.docker_commands import CommandRunner
from backend.adapters.errors import AdapterError, DeployError
from backend.adapters.http_probe import HttpProbe
from backend.adapters.image_retention import ImageRetention
from backend.adapters.override_store import OverrideStore
from backend.domain.enums import DeploymentStatus

logger = logging.getLogger(__name__)
DEPLOYMENTS_DIR_NAME = "deployments"
INTERRUPTED = "interrupted: the worker stopped while this deployment was in progress"


@dataclass(frozen=True, slots=True)
class DeployConfig:
    """Tunables of the Compose deployer."""

    health: HealthPolicy = field(default_factory=HealthPolicy)
    keep_images: int = 5
    command_timeout: float = 600.0


def _log_sink(line: str) -> None:
    logger.info("deployment recovery: %s", line)


class ComposeDeployer:
    """``Deployer`` for environments that run as a Docker Compose project."""

    def __init__(  # noqa: PLR0913 - the deployer genuinely has these collaborators
        self,
        docker: CommandRunner,
        ledger: DeploymentLedger,
        root: Path,
        probe: HttpProbe,
        *,
        config: DeployConfig | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._docker = docker
        self._ledger = ledger
        self._config = config or DeployConfig()
        self._overrides = OverrideStore(root)
        self._journal = DeployJournal(root)
        self._inspector = ContainerInspector(docker)
        verifier = HealthVerifier(self._inspector, self._config.health, probe, clock)
        self._verifier = verifier
        self._rollback = Rollback(
            docker, self._overrides, self._inspector, verifier, self._config.command_timeout
        )
        self._retention = ImageRetention(docker, self._inspector, self._config.keep_images)
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # --- deploy -------------------------------------------------------------------

    def deploy(self, request: DeployRequest, sink: LineSink, cancel: CancelSignal) -> None:
        """Roll the requested services to their new images; raise ``DeployError`` on failure
        (also when the failure was rolled back)."""
        key, files = self._target(request)
        ensure_readable(files, request.compose_env_files)
        with self._lock_for(key):
            if self._journal.exists(key):
                raise DeployError(
                    "an earlier deployment of this environment was interrupted and has not been "
                    "recovered yet"
                )
            deployment_id = self._ledger.begin(
                request.project, request.environment, request.run_id, request.commit_sha
            )
            try:
                entry = self._prepare(request, key, files, deployment_id, cancel)
            except AdapterError as error:
                self._ledger.finish(deployment_id, DeploymentStatus.FAILED, str(error))
                raise DeployError(str(error)) from error
            self._execute(entry, request, sink)

    def _target(self, request: DeployRequest) -> tuple[str, tuple[str, ...]]:
        if not request.compose_project or not request.compose_files:
            raise DeployError(
                "this environment has no Compose project and compose files configured"
            )
        if not request.services:
            raise DeployError("no services to deploy")
        names = [item.service for item in request.services]
        if len(set(names)) != len(names):
            raise DeployError("a service was named more than once")
        return request.compose_project, request.compose_files

    def _lock_for(self, key: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(key, threading.Lock())

    def _prepare(
        self,
        request: DeployRequest,
        key: str,
        files: tuple[str, ...],
        deployment_id: int,
        cancel: CancelSignal,
    ) -> JournalEntry:
        """Validate, record what runs now and journal it; nothing is changed yet."""
        project = ComposeProject(self._docker, key, files, env_files=request.compose_env_files)
        wanted = [item.service for item in request.services]
        missing = sorted(set(wanted) - project.defined_services())
        if missing:
            raise DeployError(f"not defined in the Compose project: {', '.join(missing)}")
        changes = tuple(
            self._change(project, item.service, item.image) for item in request.services
        )
        self._ledger.record_services(deployment_id, changes)
        if cancel.is_set():
            raise DeployError("cancelled before anything was changed")
        entry = JournalEntry(
            deployment_id=deployment_id,
            run_id=request.run_id,
            project=request.project,
            environment=request.environment,
            compose_project=key,
            compose_files=files,
            compose_env_files=request.compose_env_files,
            previous_override=self._overrides.current(key),
            changes=changes,
        )
        self._journal.write(entry)
        return entry

    def _change(self, project: ComposeProject, service: str, image: str) -> ServiceChange:
        new_id = self._inspector.image_id(image)
        if new_id is None:
            raise DeployError(f"image {image} is not present locally")
        running = self._inspector.containers(project.container_ids(service))
        current = running[0] if running else None
        return ServiceChange(
            service=service,
            new_image=image,
            new_image_id=new_id,
            previous_image=current.image_ref if current else None,
            previous_image_id=current.image_id if current else None,
        )

    def _execute(self, entry: JournalEntry, request: DeployRequest, sink: LineSink) -> None:
        """Apply the journalled change; on any failure roll back and raise."""
        try:
            self._apply(entry, request, sink)
        except Exception as failure:
            logger.warning("deployment failed", extra={"run_id": entry.run_id}, exc_info=True)
            self._undo(entry, str(failure) or type(failure).__name__, sink)
        self._finish_success(entry, request, sink)

    def _apply(self, entry: JournalEntry, request: DeployRequest, sink: LineSink) -> None:
        key = entry.compose_project
        images = dict(entry.previous_override)
        images.update({c.service: c.new_image for c in entry.changes})
        self._overrides.write(key, images)
        project = ComposeProject(
            self._docker,
            key,
            entry.compose_files,
            self._overrides.path(key),
            entry.compose_env_files,
        )
        services = [c.service for c in entry.changes]
        for change in entry.changes:
            before = change.previous_image or "(none)"
            sink(f"deploying {change.service}: {before} -> {change.new_image}")
        result = project.up(services, sink, self._config.command_timeout)
        if not result.ok:
            raise DeployError(f"docker compose up failed (exit code {result.exit_code})")
        self._verifier.verify(project, services, sink, request.http_check)

    def _undo(self, entry: JournalEntry, reason: str, sink: LineSink) -> None:
        """Roll back and raise; the deployment is recorded as rolled back or rollback failed."""
        sink(f"deployment failed: {reason}")
        result = self._rollback.run(entry, sink)
        if result.ok:
            message = f"deployment failed ({reason}); rolled back to the previous images"
            status = DeploymentStatus.ROLLED_BACK
        else:
            message = (
                f"ROLLBACK FAILED, manual intervention needed: {result.message} "
                f"(deployment failed: {reason})"
            )
            status = DeploymentStatus.ROLLBACK_FAILED
        self._ledger.finish(entry.deployment_id, status, message)
        self._journal.remove(entry.compose_project)
        raise DeployError(message)

    def _finish_success(self, entry: JournalEntry, request: DeployRequest, sink: LineSink) -> None:
        self._ledger.finish(entry.deployment_id, DeploymentStatus.SUCCEEDED, None)
        self._journal.remove(entry.compose_project)
        sink("deployment succeeded")
        try:
            protected = self._ledger.protected_images(request.project, request.environment)
            self._retention.prune(request.project, request.environment, protected, sink)
        except Exception:
            logger.warning("image retention failed", extra={"run_id": entry.run_id}, exc_info=True)
            sink("image cleanup failed; see the worker log")

    # --- recovery -----------------------------------------------------------------

    def recover(self) -> list[int]:
        """Roll back every deployment whose journal survived a worker crash.

        Rolling back (rather than finishing) is deliberate: the run that owned the deployment
        is marked interrupted, so the environment must end up on the images it had before.
        Returns the deployment ids handled; deployments left ``deploying`` without a journal
        (the worker died before changing anything) are marked failed.
        """
        handled: list[int] = []
        for entry in self._journal.pending():
            with self._lock_for(entry.compose_project):
                result = self._rollback.run(entry, _log_sink)
                status = (
                    DeploymentStatus.ROLLED_BACK if result.ok else DeploymentStatus.ROLLBACK_FAILED
                )
                self._ledger.finish(entry.deployment_id, status, f"{INTERRUPTED}; {result.message}")
                self._journal.remove(entry.compose_project)
                handled.append(entry.deployment_id)
        handled.extend(self._ledger.fail_unfinished(INTERRUPTED))
        return handled
