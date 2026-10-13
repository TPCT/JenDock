"""Deployment records: the ledger the deployer writes, history and redeploy for the API."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.exc import IntegrityError

from backend.adapters.deploy_ledger import ServiceChange
from backend.domain.actor import Actor
from backend.domain.audit import AuditEntry, AuditTarget
from backend.domain.clock import Clock, utcnow
from backend.domain.enums import (
    ActorType,
    DeploymentStatus,
    EnvironmentName,
    PipelineSource,
    RunStatus,
    RunTrigger,
)
from backend.domain.errors import ConflictError, NotFoundError
from backend.domain.paging import Page, PageQuery
from backend.repositories.database import UnitOfWork
from backend.repositories.models import Deployment, Run
from backend.services.audit import add_audit_event
from backend.services.lookup import require_environment
from backend.services.run_lifecycle import clip_message

UowFactory = Callable[[], UnitOfWork]
MAX_PAGE_SIZE = 100
REDEPLOY_STAGE = "Redeploy"


def logical_name(index: int) -> str:
    """The logical image name a redeploy pipeline uses for the ``index``-th service."""
    return f"svc{index}"


def redeploy_pipeline(services: Sequence[str]) -> str:
    """The synthetic Jenkinsfile of a redeploy: one ``deploy`` of already built images."""
    entries = ", ".join(f"'{name}': '{logical_name(i)}'" for i, name in enumerate(services))
    return (
        "pipeline {\n"
        "    agent any\n"
        "    stages {\n"
        f"        stage('{REDEPLOY_STAGE}') {{\n"
        "            steps {\n"
        f"                deploy(services: [{entries}])\n"
        "            }\n"
        "        }\n"
        "    }\n"
        "}\n"
    )


def redeploy_images(uow: UnitOfWork, run_id: int) -> dict[str, str]:
    """Logical image name to tag for the pending redeploy of a run (the unit of work must
    be active); empty when the run is not a redeploy."""
    pending = uow.deployments.pending_for_run(run_id)
    if pending is None:
        return {}
    changes = [ServiceChange.from_json(item) for item in pending.services]
    return {logical_name(index): change.new_image for index, change in enumerate(changes)}


class DatabaseLedger:
    """``DeploymentLedger`` over the database; one short unit of work per call."""

    def __init__(self, uow_factory: UowFactory, clock: Clock = utcnow) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    def begin(self, project: str, environment: str, run_id: int, commit_sha: str) -> int:
        """Record a deployment as ``deploying``; a pending redeploy of the run is adopted."""
        now = self._clock()
        with self._uow_factory() as uow:
            pending = uow.deployments.pending_for_run(run_id)
            if pending is not None:
                pending.status = DeploymentStatus.DEPLOYING
                pending.started_at = now
                return pending.id
            env = require_environment(uow, project, EnvironmentName(environment))
            deployment = Deployment(
                run_id=run_id,
                environment_id=env.id,
                status=DeploymentStatus.DEPLOYING,
                commit_sha=commit_sha,
                services=[],
                created_at=now,
                started_at=now,
            )
            return uow.deployments.add(deployment).id

    def record_services(self, deployment_id: int, changes: Sequence[ServiceChange]) -> None:
        """Store the previous and new image of every service."""
        with self._uow_factory() as uow:
            deployment = _require(uow, deployment_id)
            deployment.services = [change.to_json() for change in changes]

    def finish(self, deployment_id: int, status: DeploymentStatus, message: str | None) -> None:
        """Record the final status."""
        with self._uow_factory() as uow:
            deployment = _require(uow, deployment_id)
            deployment.status = status
            deployment.message = clip_message(message) if message else None
            deployment.finished_at = self._clock()

    def fail_unfinished(self, message: str) -> list[int]:
        """Mark every deployment still ``deploying`` as failed."""
        now = self._clock()
        with self._uow_factory() as uow:
            rows = list(uow.deployments.list_by_status(DeploymentStatus.DEPLOYING))
            for row in rows:
                row.status = DeploymentStatus.FAILED
                row.message = clip_message(message)
                row.finished_at = now
            return [row.id for row in rows]

    def protected_images(self, project: str, environment: str) -> frozenset[str]:
        """Images of the last deployment (previous and new) and of the last successful one."""
        with self._uow_factory() as uow:
            env = require_environment(uow, project, EnvironmentName(environment))
            rows = list(uow.deployments.list_for_environment(env.id, limit=50, offset=0))
        latest = next((r for r in rows if r.status is not DeploymentStatus.PENDING), None)
        succeeded = next((r for r in rows if r.status is DeploymentStatus.SUCCEEDED), None)
        protected: set[str] = set()
        for row in (latest, succeeded):
            for item in row.services if row else ():
                protected.update(v for v in item.values() if v)
        protected.discard("")
        return frozenset(protected)


def _require(uow: UnitOfWork, deployment_id: int) -> Deployment:
    deployment = uow.deployments.get(deployment_id)
    if deployment is None:
        raise NotFoundError(f"deployment not found: {deployment_id}")
    return deployment


@dataclass(frozen=True, slots=True)
class DeploymentView:
    """A deployment as the history shows it."""

    id: int
    run_id: int
    run_number: int
    status: DeploymentStatus
    commit_sha: str
    services: tuple[ServiceChange, ...]
    message: str | None
    redeploy_of_id: int | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


@dataclass(frozen=True, slots=True)
class RedeployResult:
    """The queued run of a redeploy."""

    run: Run
    project: str
    environment: str
    deployment_id: int


@dataclass(frozen=True, slots=True)
class RunningImage:
    """The image a service is believed to run now, and the deployment that decided it."""

    service: str
    image: str
    deployment_id: int


@dataclass(frozen=True, slots=True)
class RedeployPreview:
    """What a redeploy would do: the images it deploys, what runs now, and whether another
    deployment of the environment is still in progress (which refuses the redeploy)."""

    target: DeploymentView
    running: tuple[RunningImage, ...]
    in_progress: bool


class DeploymentService:
    """Deployment history and redeploy of earlier versions."""

    def __init__(self, uow: UnitOfWork, clock: Clock = utcnow) -> None:
        self._uow = uow
        self._clock = clock

    def history(
        self,
        slug: str,
        name: EnvironmentName,
        query: PageQuery,
        status: DeploymentStatus | None = None,
    ) -> Page[DeploymentView]:
        """A page of an environment's deployments (default order: newest first)."""
        with self._uow:
            env = require_environment(self._uow, slug, name)
            rows, total = self._uow.deployments.search_for_environment(env.id, query, status)
            return Page.of((self._view(row) for row in rows), total, query)

    def _view(self, row: Deployment) -> DeploymentView:
        run = self._uow.runs.get(row.run_id)
        status, message = row.status, row.message
        if row.status is DeploymentStatus.PENDING and run is not None and run.status.is_finished:
            # The redeploy run ended without ever reaching the deployer.
            status, message = DeploymentStatus.FAILED, run.message or "the run ended"
        return DeploymentView(
            id=row.id,
            run_id=row.run_id,
            run_number=run.number if run else 0,
            status=status,
            commit_sha=row.commit_sha,
            services=tuple(ServiceChange.from_json(item) for item in row.services),
            message=message,
            redeploy_of_id=row.redeploy_of_id,
            created_at=row.created_at,
            started_at=row.started_at,
            finished_at=row.finished_at,
        )

    def preview_redeploy(
        self, slug: str, name: EnvironmentName, deployment_id: int
    ) -> RedeployPreview:
        """What redeploying ``deployment_id`` would deploy and replace; changes nothing.

        Refused like the redeploy itself when the deployment is unknown or did not succeed.
        """
        with self._uow:
            env = require_environment(self._uow, slug, name)
            source = self._source(env.id, deployment_id)
            recent = [
                self._view(row)
                for row in self._uow.deployments.list_for_environment(
                    env.id, limit=MAX_PAGE_SIZE, offset=0
                )
            ]
            return RedeployPreview(self._view(source), _running_images(recent), _busy(recent))

    def redeploy(
        self,
        slug: str,
        name: EnvironmentName,
        deployment_id: int,
        actor: Actor,
        *,
        refuse_in_progress: bool = False,
    ) -> RedeployResult:
        """Queue a run that deploys the images of an earlier successful deployment again,
        without rebuilding; audited. Concurrent creators racing for a run number get
        ``ConflictError`` and may retry."""
        try:
            with self._uow:
                env = require_environment(self._uow, slug, name)
                source = self._source(env.id, deployment_id)
                if refuse_in_progress and self._in_progress(env.id):
                    raise ConflictError("a deployment of this environment is still in progress")
                run = self._queue_run(env.id, source, actor)
                pending = self._pending(env.id, run, source)
                details: dict[str, object] = {
                    "run_id": run.id,
                    "run_number": run.number,
                    "services": [item["service"] for item in source.services],
                }
                entry = AuditEntry(
                    actor, "deployment.redeploy", AuditTarget("deployment", str(source.id)), details
                )
                add_audit_event(self._uow, entry, self._clock)
                return RedeployResult(run, slug, name.value, pending.id)
        except IntegrityError as error:
            raise ConflictError("run number was taken concurrently") from error

    def _in_progress(self, environment_id: int) -> bool:
        rows = self._uow.deployments.list_for_environment(
            environment_id, limit=MAX_PAGE_SIZE, offset=0
        )
        return _busy([self._view(row) for row in rows])

    def _source(self, environment_id: int, deployment_id: int) -> Deployment:
        source = self._uow.deployments.get(deployment_id)
        if source is None or source.environment_id != environment_id:
            raise NotFoundError(f"deployment not found: {deployment_id}")
        if source.status is not DeploymentStatus.SUCCEEDED:
            raise ConflictError(f"only a succeeded deployment can be redeployed ({source.status})")
        return source

    def _queue_run(self, environment_id: int, source: Deployment, actor: Actor) -> Run:
        origin = self._uow.runs.get(source.run_id)
        services = [str(item["service"]) for item in source.services]
        user_id = int(actor.id) if actor.type is ActorType.USER and actor.id else None
        token_id = int(actor.id) if actor.type is ActorType.TOKEN and actor.id else None
        run = Run(
            environment_id=environment_id,
            number=self._uow.runs.last_number(environment_id) + 1,
            trigger=RunTrigger.REDEPLOY,
            branch=origin.branch if origin else "redeploy",
            commit_sha=source.commit_sha,
            pipeline_source=PipelineSource.PLATFORM,
            pipeline_text=redeploy_pipeline(services),
            status=RunStatus.QUEUED,
            triggered_by=user_id,
            triggered_by_token_id=token_id,
            created_at=self._clock(),
        )
        return self._uow.runs.add(run)

    def _pending(self, environment_id: int, run: Run, source: Deployment) -> Deployment:
        changes = [
            ServiceChange(
                service=str(item["service"]),
                new_image=str(item["new_image"]),
                new_image_id=item.get("new_image_id"),
            )
            for item in source.services
        ]
        pending = Deployment(
            run_id=run.id,
            environment_id=environment_id,
            status=DeploymentStatus.PENDING,
            commit_sha=source.commit_sha,
            services=[change.to_json() for change in changes],
            redeploy_of_id=source.id,
            created_at=self._clock(),
        )
        return self._uow.deployments.add(pending)


def _busy(views: Sequence[DeploymentView]) -> bool:
    """Whether a deployment is running or a redeploy is queued and has not ended."""
    return any(v.status in (DeploymentStatus.DEPLOYING, DeploymentStatus.PENDING) for v in views)


def _running_images(views: Sequence[DeploymentView]) -> tuple[RunningImage, ...]:
    """Per service, the image of the newest deployment that settled it: a succeeded one runs its
    new image, a failed or rolled-back one left the previous image running."""
    found: dict[str, RunningImage] = {}
    for view in views:  # newest first
        for change in view.services:
            if change.service in found:
                continue
            if view.status is DeploymentStatus.SUCCEEDED:
                found[change.service] = RunningImage(change.service, change.new_image, view.id)
            elif (
                view.status in (DeploymentStatus.ROLLED_BACK, DeploymentStatus.FAILED)
                and change.previous_image
            ):
                found[change.service] = RunningImage(change.service, change.previous_image, view.id)
    return tuple(sorted(found.values(), key=lambda item: item.service))
