"""Read models of projects and run lists for the API."""

from __future__ import annotations

from dataclasses import dataclass

from shiftcodes.domain.enums import EnvironmentName
from shiftcodes.domain.errors import NotFoundError
from shiftcodes.repositories.database import UnitOfWork
from shiftcodes.repositories.models import Environment, Project, Run
from shiftcodes.services.lookup import require_environment, require_project


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


@dataclass(frozen=True, slots=True)
class ProjectView:
    """A project with its environments (development first)."""

    project: Project
    environments: tuple[EnvironmentView, ...]


class ProjectQueryService:
    """Loads projects and run lists for display."""

    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    def list_projects(self) -> list[ProjectView]:
        """Active projects ordered by name."""
        with self._uow:
            projects = sorted(self._uow.projects.list(), key=lambda p: (p.name.lower(), p.slug))
            return [self._view(project) for project in projects]

    def get(self, slug: str) -> ProjectView:
        """One active project; archived projects are not found."""
        with self._uow:
            project = require_project(self._uow, slug)
            if project.archived_at is not None:
                raise NotFoundError(f"project not found: {slug}")
            return self._view(project)

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

    def all_runs(self, *, limit: int, offset: int) -> RunPage:
        """A page of the runs of every project and environment."""
        with self._uow:
            return self._page(None, limit, offset)

    def _page(self, environment_id: int | None, limit: int, offset: int) -> RunPage:
        rows = self._uow.runs.page(environment_id=environment_id, limit=limit, offset=offset)
        items = tuple(RunSummaryData(run, slug, name.value) for run, slug, name in rows)
        total = self._uow.runs.count(environment_id=environment_id)
        return RunPage(items, total, limit, offset)

    def _view(self, project: Project) -> ProjectView:
        environments = self._uow.environments.list_for_project(project.id)
        return ProjectView(project, tuple(self._environment_view(project, e) for e in environments))

    def _environment_view(self, project: Project, environment: Environment) -> EnvironmentView:
        latest = self._uow.runs.latest_for_environment(environment.id)
        summary = RunSummaryData(latest, project.slug, environment.name.value) if latest else None
        return EnvironmentView(environment, summary)
