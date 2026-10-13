"""Project use cases."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError

from shiftcodes.domain.actor import Actor
from shiftcodes.domain.audit import AuditEntry, AuditTarget
from shiftcodes.domain.clock import Clock, utcnow
from shiftcodes.domain.enums import EnvironmentName, GitTransport
from shiftcodes.domain.errors import ConflictError, ValidationError
from shiftcodes.domain.values import (
    BranchName,
    GitHubRepository,
    ProjectSlug,
    validate_display_name,
)
from shiftcodes.repositories.database import UnitOfWork
from shiftcodes.repositories.models import Environment, Project
from shiftcodes.services.audit import add_audit_event
from shiftcodes.services.lookup import require_project


@dataclass(frozen=True, slots=True)
class ProjectDetail:
    """A project together with its two environments."""

    project: Project
    environments: Sequence[Environment]


class ProjectService:
    """Create, read, update and archive projects."""

    def __init__(self, uow: UnitOfWork, clock: Clock = utcnow) -> None:
        self._uow = uow
        self._clock = clock

    def create(  # noqa: PLR0913, PLR0917 - a project genuinely has these inputs
        self,
        slug: str,
        name: str,
        repository: str,
        development_branch: str = "development",
        production_branch: str = "production",
        git_transport: GitTransport = GitTransport.HTTPS,
        actor: Actor | None = None,
    ) -> ProjectDetail:
        """Create a project and exactly its development and production environments.

        With an ``actor`` the creation is audited (``project.create``) in the same transaction.
        """
        valid_slug = ProjectSlug(slug)
        branches = {
            EnvironmentName.DEVELOPMENT: BranchName(development_branch),
            EnvironmentName.PRODUCTION: BranchName(production_branch),
        }
        if development_branch == production_branch:
            raise ValidationError("development and production must use different branches")
        project = Project(
            slug=str(valid_slug),
            name=validate_display_name(name),
            repository=str(GitHubRepository(repository)),
            git_transport=git_transport,
            created_at=self._clock(),
        )
        try:
            with self._uow:
                detail = self._create_in_transaction(project, branches)
                if actor is not None:
                    self._audit_create(detail, actor)
                return detail
        except IntegrityError as error:
            if _violates_slug_unique(error):
                raise ConflictError(f"project slug already exists: {slug}") from error
            raise ConflictError(
                "project could not be created: a constraint was violated"
            ) from error

    def _create_in_transaction(
        self, project: Project, branches: dict[EnvironmentName, BranchName]
    ) -> ProjectDetail:
        if self._uow.projects.get_by_slug(project.slug) is not None:
            raise ConflictError(f"project slug already exists: {project.slug}")
        self._uow.projects.add(project)
        environments = [
            self._uow.environments.add(self._new_environment(project, name, str(branch)))
            for name, branch in branches.items()
        ]
        return ProjectDetail(project, environments)

    def _audit_create(self, detail: ProjectDetail, actor: Actor) -> None:
        project = detail.project
        details: dict[str, object] = {
            "slug": project.slug,
            "repository": project.repository,
            "git_transport": project.git_transport.value,
            "branches": {e.name.value: e.branch for e in detail.environments},
        }
        entry = AuditEntry(actor, "project.create", AuditTarget("project", project.slug), details)
        add_audit_event(self._uow, entry, self._clock)

    def _new_environment(self, project: Project, name: EnvironmentName, branch: str) -> Environment:
        now = self._clock()
        return Environment(
            project_id=project.id,
            name=name,
            branch=branch,
            auto_deploy=False,
            compose_files=[],
            services=[],
            created_at=now,
            updated_at=now,
        )

    def get(self, slug: str) -> ProjectDetail:
        """Return a project (archived included) with its environments."""
        with self._uow:
            project = require_project(self._uow, slug)
            return ProjectDetail(project, self._uow.environments.list_for_project(project.id))

    def list(self, *, include_archived: bool = False) -> Sequence[Project]:
        """Return projects, excluding archived ones unless requested."""
        with self._uow:
            return self._uow.projects.list(include_archived=include_archived)

    def update(
        self,
        slug: str,
        *,
        name: str | None = None,
        repository: str | None = None,
        git_transport: GitTransport | None = None,
        actor: Actor | None = None,
    ) -> Project:
        """Change the display name, repository and/or git transport; ``None`` leaves a field
        unchanged. With an ``actor`` the change is audited (``project.update``)."""
        with self._uow:
            project = require_project(self._uow, slug)
            changes: dict[str, object] = {}
            if name is not None:
                project.name = validate_display_name(name)
                changes["name"] = project.name
            if repository is not None:
                project.repository = str(GitHubRepository(repository))
                changes["repository"] = project.repository
            if git_transport is not None:
                project.git_transport = git_transport
                changes["git_transport"] = git_transport.value
            if actor is not None and changes:
                target = AuditTarget("project", project.slug)
                entry = AuditEntry(actor, "project.update", target, {"changes": changes})
                add_audit_event(self._uow, entry, self._clock)
            return project

    def archive(self, slug: str) -> Project:
        """Archive a project; archiving twice keeps the first archive time."""
        with self._uow:
            project = require_project(self._uow, slug)
            if project.archived_at is None:
                project.archived_at = self._clock()
            return project


def _violates_slug_unique(error: IntegrityError) -> bool:
    """Recognize the projects.slug unique violation on SQLite and PostgreSQL."""
    message = str(error.orig)
    return "projects.slug" in message or "uq_projects_slug" in message
