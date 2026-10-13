"""Project, environment, branch head and run trigger schemas."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from backend.api.schemas.runs import RunSummary
from backend.domain.enums import GitTransport
from backend.domain.git import BranchHead
from backend.services.project_queries import EnvironmentView, ProjectView


class EnvironmentResponse(BaseModel):
    """An environment with its newest run."""

    name: str
    branch: str
    auto_deploy: bool
    compose_project: str | None
    compose_files: list[str]
    compose_env_files: list[str]
    services: list[str]
    last_run: RunSummary | None

    @classmethod
    def from_view(cls, view: EnvironmentView) -> EnvironmentResponse:
        """Build from the environment read model."""
        environment = view.environment
        return cls(
            name=environment.name.value,
            branch=environment.branch,
            auto_deploy=environment.auto_deploy,
            compose_project=environment.compose_project,
            compose_files=list(environment.compose_files),
            compose_env_files=list(environment.compose_env_files),
            services=list(environment.services),
            last_run=RunSummary.from_data(view.last_run) if view.last_run else None,
        )


class ProjectResponse(BaseModel):
    """A project with exactly its development and production environments."""

    slug: str
    name: str
    repository: str
    git_transport: GitTransport
    created_at: datetime
    environments: list[EnvironmentResponse]

    @classmethod
    def from_view(cls, view: ProjectView) -> ProjectResponse:
        """Build from the project read model."""
        project = view.project
        return cls(
            slug=project.slug,
            name=project.name,
            repository=project.repository,
            git_transport=project.git_transport,
            created_at=project.created_at,
            environments=[EnvironmentResponse.from_view(e) for e in view.environments],
        )


class CreateProjectRequest(BaseModel):
    """Body of ``POST /api/projects``."""

    model_config = ConfigDict(extra="forbid")

    slug: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=120)
    repository: str = Field(min_length=3, max_length=160)
    development_branch: str = Field(default="development", min_length=1, max_length=200)
    production_branch: str = Field(default="production", min_length=1, max_length=200)
    git_transport: GitTransport = GitTransport.SSH


class UpdateProjectRequest(BaseModel):
    """Body of ``PATCH /api/projects/{slug}``; at least one field."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=120)
    repository: str | None = Field(default=None, min_length=3, max_length=160)
    git_transport: GitTransport | None = None

    @model_validator(mode="after")
    def _require_a_change(self) -> UpdateProjectRequest:
        if self.name is None and self.repository is None and self.git_transport is None:
            raise ValueError("at least one field is required")
        return self


class UpdateEnvironmentRequest(BaseModel):
    """Body of ``PATCH /api/projects/{slug}/environments/{env}``; at least one field."""

    model_config = ConfigDict(extra="forbid")

    branch: str | None = Field(default=None, min_length=1, max_length=200)
    auto_deploy: bool | None = None
    # The Docker Compose project the platform deploys to and manages. ``null`` clears the name;
    # the file lists replace the stored ones; every path is absolute (README "Deployment").
    compose_project: str | None = Field(default=None, max_length=63)
    compose_files: list[str] | None = Field(default=None, max_length=16)
    compose_env_files: list[str] | None = Field(default=None, max_length=16)
    services: list[str] | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _require_a_change(self) -> UpdateEnvironmentRequest:
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        for name in ("branch", "auto_deploy", "compose_files", "compose_env_files", "services"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self

    @property
    def clears_compose_project(self) -> bool:
        """Whether the request names ``compose_project`` explicitly as null."""
        return "compose_project" in self.model_fields_set and self.compose_project is None


class BranchHeadResponse(BaseModel):
    """The head of a branch, with its commit details."""

    branch: str
    commit_sha: str
    message: str
    author: str
    committed_at: datetime | None

    @classmethod
    def from_head(cls, head: BranchHead) -> BranchHeadResponse:
        """Build from the head returned by the worker (times are reported in UTC)."""
        committed = head.committed_at.astimezone(UTC) if head.committed_at else None
        return cls(
            branch=head.branch,
            commit_sha=head.commit_sha,
            message=head.message,
            author=head.author,
            committed_at=committed,
        )


class ManualRunRequest(BaseModel):
    """Body of ``POST .../environments/{env}/runs``: the commit the user previewed."""

    model_config = ConfigDict(extra="forbid")

    branch: str = Field(min_length=1, max_length=200)
    commit_sha: str = Field(min_length=40, max_length=40)
