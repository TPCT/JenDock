"""Human-started runs: a manual run of a confirmed commit, and a retry of an earlier run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.exc import IntegrityError

from backend.domain.actor import Actor
from backend.domain.clock import Clock, utcnow
from backend.domain.enums import EnvironmentName, RunTrigger
from backend.domain.errors import ConflictError, NotFoundError
from backend.domain.git import BranchHead
from backend.domain.values import BranchName, CommitSha
from backend.repositories.database import UnitOfWork
from backend.repositories.models import Run
from backend.services.lookup import require_environment
from backend.services.runs import RunRequest, queue_run

SHORT_SHA = 12


class HeadLookup(Protocol):
    """Asks the worker for the current head of a branch (the API never runs git)."""

    def branch_head(
        self, slug: str, branch: str, actor: Actor, *, details: bool = False
    ) -> BranchHead:
        """Return the head; raise ``NotFoundError`` when the branch does not exist."""


@dataclass(frozen=True, slots=True)
class RunOrigin:
    """Who asked for a run: the audit actor and the id to record on the run row."""

    actor: Actor
    user_id: int | None = None
    token_id: int | None = None


@dataclass(frozen=True, slots=True)
class _Target:
    """The environment and the commit a new run is for."""

    slug: str
    name: EnvironmentName
    branch: str
    commit_sha: str


class RunTriggerService:
    """Creates ``manual`` runs; every creation is audited as ``run.create``."""

    def __init__(self, uow: UnitOfWork, heads: HeadLookup, clock: Clock = utcnow) -> None:
        self._uow = uow
        self._heads = heads
        self._clock = clock

    def manual(
        self,
        slug: str,
        name: EnvironmentName,
        branch: str,
        commit_sha: str,
        origin: RunOrigin,
    ) -> Run:
        """Queue a run of ``commit_sha`` if it is still the head of ``branch``.

        The user confirmed a specific commit; when the branch moved since, nothing is queued
        (``ConflictError``) so they can look at the new head first.
        """
        valid_branch = str(BranchName(branch))
        valid_sha = str(CommitSha(commit_sha))
        with self._uow:
            require_environment(self._uow, slug, name)
        head = self._heads.branch_head(slug, valid_branch, origin.actor)
        if head.commit_sha != valid_sha:
            raise ConflictError(
                f"branch {valid_branch} moved: its head is now {head.commit_sha[:SHORT_SHA]}, "
                f"not the confirmed {valid_sha[:SHORT_SHA]}"
            )
        return self._queue(_Target(slug, name, valid_branch, valid_sha), origin, {})

    def retry(self, run_id: int, origin: RunOrigin) -> Run:
        """Queue a new run of the same commit and branch; the original must be finished."""
        with self._uow:
            original = self._uow.runs.get(run_id)
            if original is None:
                raise NotFoundError(f"run not found: {run_id}")
            if not original.status.is_finished:
                raise ConflictError(f"run {run_id} is still {original.status.value}")
            environment = self._uow.environments.get(original.environment_id)
            project = self._uow.projects.get(environment.project_id) if environment else None
            if environment is None or project is None:
                raise NotFoundError(f"environment of run {run_id} not found")
            slug, name = project.slug, environment.name
            branch, sha = original.branch, original.commit_sha
        target = _Target(slug, name, branch, sha)
        return self._queue(target, origin, {"retry_of": run_id})

    def _queue(self, target: _Target, origin: RunOrigin, details: dict[str, object]) -> Run:
        request = RunRequest(
            RunTrigger.MANUAL,
            target.branch,
            target.commit_sha,
            triggered_by=origin.user_id,
            triggered_by_token_id=origin.token_id,
            audit_actor=origin.actor,
            audit_details=details,
        )
        try:
            with self._uow:
                environment = require_environment(self._uow, target.slug, target.name)
                return queue_run(self._uow, self._clock, environment, request)
        except IntegrityError as error:
            raise ConflictError("run number was taken concurrently; try again") from error
