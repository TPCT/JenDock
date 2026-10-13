"""Read models of projects and run lists for the API."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from backend.domain.enums import EnvironmentName
from backend.domain.errors import NotFoundError
from backend.repositories.database import UnitOfWork
from backend.repositories.models import Environment, Project, Run
from backend.services.lookup import require_environment, require_project


@dataclass(frozen=True, slots=True)
class RunSummaryData:
    """A run with the names of its project and environment (no stages)."""

    run: Run
    project: str
    environment: str


@dataclass(frozen=True, slots=True)
class RunPage:
    """One page of runs, newest first."""

    items: tuple[RunSummaryData, ...]
    total: int
    limit: int
    offset: int


@dataclass(frozen=True, slots=True)
class EnvironmentView:
    """An environment with its newest run."""

    environment: Environment
    last_run: RunSummaryData | None
    has_pipeline: bool = False


@dataclass(frozen=True, slots=True)
class ProjectView:
    """A project with its environments (development first)."""

    project: Project
    environments: tuple[EnvironmentView, ...]


EnvironmentPredicate = Callable[[str, EnvironmentName], bool]


@dataclass(frozen=True, slots=True)
class ProjectFilter:
    """What a caller may see: environments it can know of, and those whose runs it may read."""

    sees: EnvironmentPredicate
    reads_runs: EnvironmentPredicate


class ProjectQueryService:
    """Loads projects and run lists for display."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    def list_projects(self, only: ProjectFilter | None = None) -> list[ProjectView]:
        """Active projects ordered by name; with ``only``, just what the caller may see."""
        with self._uow:
            projects = sorted(self._uow.projects.list(), key=lambda p: (p.name.lower(), p.slug))
            views = [self._view(project, only) for project in projects]
            return [view for view in views if only is None or view.environments]

    def get(self, slug: str, only: ProjectFilter | None = None) -> ProjectView:
        """One active project; archived projects are not found."""
        with self._uow:
            project = require_project(self._uow, slug)
            if project.archived_at is not None:
                raise NotFoundError(f"project not found: {slug}")
            return self._view(project, only)

    def environment(self, slug: str, name: EnvironmentName) -> EnvironmentView:
        """One environment of an active project with its newest run."""
        with self._uow:
            project = require_project(self._uow, slug)
            if project.archived_at is not None:
                raise NotFoundError(f"project not found: {slug}")
            return self._environment_view(project, require_environment(self._uow, slug, name))

    def environment_runs(
        self, slug: str, name: EnvironmentName, *, limit: int, offset: int
    ) -> RunPage:
        """A page of the runs of one environment."""
        with self._uow:
            environment = require_environment(self._uow, slug, name)
            return self._page(environment.id, limit, offset)

    def all_runs(
        self, *, limit: int, offset: int, only: EnvironmentPredicate | None = None
    ) -> RunPage:
        """A page of the runs of every project and environment (or of those ``only`` allows)."""
        with self._uow:
            ids = None if only is None else self._allowed_environment_ids(only)
            return self._page(None, limit, offset, ids)

    def _allowed_environment_ids(self, allowed: EnvironmentPredicate) -> frozenset[int]:
        found = set()
        for project in self._uow.projects.list():
            for environment in self._uow.environments.list_for_project(project.id):
                if allowed(project.slug, environment.name):
                    found.add(environment.id)
        return frozenset(found)

    def _page(
        self,
        environment_id: int | None,
        limit: int,
        offset: int,
        environment_ids: frozenset[int] | None = None,
    ) -> RunPage:
        rows = self._uow.runs.page(
            environment_id=environment_id,
            limit=limit,
            offset=offset,
            environment_ids=environment_ids,
        )
        items = tuple(RunSummaryData(run, slug, name.value) for run, slug, name in rows)
        total = self._uow.runs.count(environment_id=environment_id, environment_ids=environment_ids)
        return RunPage(items, total, limit, offset)

    def _view(self, project: Project, only: ProjectFilter | None = None) -> ProjectView:
        environments = self._uow.environments.list_for_project(project.id)
        if only is not None:
            environments = [e for e in environments if only.sees(project.slug, e.name)]
        views = tuple(
            self._environment_view(
                project, e, only is None or only.reads_runs(project.slug, e.name)
            )
            for e in environments
        )
        return ProjectView(project, views)

    def _environment_view(
        self, project: Project, environment: Environment, with_runs: bool = True
    ) -> EnvironmentView:
        latest = self._uow.runs.latest_for_environment(environment.id) if with_runs else None
        summary = RunSummaryData(latest, project.slug, environment.name.value) if latest else None
        has_pipeline = self._uow.pipelines.get_latest(environment.id) is not None
        return EnvironmentView(environment, summary, has_pipeline)
