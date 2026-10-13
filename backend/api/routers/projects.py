"""Project endpoints: list, create, settings, environment runs, branch head and manual runs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from backend.api import authz
from backend.api.deps import Container, Target, require
from backend.api.principal import Principal
from backend.api.schemas.projects import (
    BranchHeadResponse,
    CreateProjectRequest,
    EnvironmentResponse,
    ManualRunRequest,
    ProjectResponse,
    UpdateEnvironmentRequest,
    UpdateProjectRequest,
)
from backend.api.schemas.runs import RunPage, RunSummary
from backend.domain.access import Action
from backend.domain.enums import EnvironmentName
from backend.domain.values import BranchName
from backend.services.environments import EnvironmentUpdate

router = APIRouter(prefix="/api/projects", tags=["projects"])
Lister = Annotated[Principal, Depends(require(Action.PROJECT_LIST, Target.NONE))]
Creator = Annotated[Principal, Depends(require(Action.PROJECT_CREATE, Target.NONE))]
Reader = Annotated[Principal, Depends(require(Action.PROJECT_READ, Target.PROJECT))]
Editor = Annotated[Principal, Depends(require(Action.PROJECT_UPDATE, Target.PROJECT))]
EnvEditor = Annotated[Principal, Depends(require(Action.ENVIRONMENT_UPDATE))]
RunReader = Annotated[Principal, Depends(require(Action.RUN_READ))]
Runner = Annotated[Principal, Depends(require(Action.RUN_CREATE))]
MAX_PAGE = 100


@dataclass(frozen=True, slots=True)
class EnvPath:
    """Path parameters identifying an environment."""

    slug: str
    env: EnvironmentName


@router.get("")
def list_projects(principal: Lister, container: Container) -> list[ProjectResponse]:
    """Projects the caller can see, ordered by name, each with its environments and newest runs."""
    views = container.project_queries().list_projects(authz.project_filter(principal))
    return [ProjectResponse.from_view(view) for view in views]


@router.post("", status_code=201)
def create_project(
    body: CreateProjectRequest, principal: Creator, container: Container
) -> ProjectResponse:
    """Create a project with its development and production environments."""
    container.project_service().create(
        body.slug,
        body.name,
        body.repository,
        body.development_branch,
        body.production_branch,
        git_transport=body.git_transport,
        actor=principal.actor,
    )
    return ProjectResponse.from_view(container.project_queries().get(body.slug))


@router.get("/{slug}")
def get_project(slug: str, principal: Reader, container: Container) -> ProjectResponse:
    """One project; archived projects are not found."""
    view = container.project_queries().get(slug, authz.project_filter(principal))
    return ProjectResponse.from_view(view)


@router.patch("/{slug}")
def update_project(
    slug: str, body: UpdateProjectRequest, principal: Editor, container: Container
) -> ProjectResponse:
    """Change the name, repository or git transport (ssh deploy key or public https)."""
    container.project_service().update(
        slug,
        name=body.name,
        repository=body.repository,
        git_transport=body.git_transport,
        actor=principal.actor,
    )
    return ProjectResponse.from_view(container.project_queries().get(slug))


@router.patch("/{slug}/environments/{env}")
def update_environment(
    path: Annotated[EnvPath, Depends()],
    body: UpdateEnvironmentRequest,
    principal: EnvEditor,
    container: Container,
) -> EnvironmentResponse:
    """Change the branch, auto-deploy and the Compose project, files, env files and services."""
    change = EnvironmentUpdate(
        branch=body.branch,
        auto_deploy=body.auto_deploy,
        compose_project=body.compose_project,
        clear_compose_project=body.clears_compose_project,
        compose_files=body.compose_files,
        compose_env_files=body.compose_env_files,
        services=body.services,
    )
    container.environment_service().update(path.slug, path.env, change, principal.actor)
    return EnvironmentResponse.from_view(
        container.project_queries().environment(path.slug, path.env)
    )


@router.get("/{slug}/environments/{env}/runs")
def list_environment_runs(
    path: Annotated[EnvPath, Depends()],
    _principal: RunReader,
    container: Container,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RunPage:
    """Runs of one environment, newest first."""
    page = container.project_queries().environment_runs(
        path.slug, path.env, limit=limit, offset=offset
    )
    return RunPage.from_page(page)


@router.get("/{slug}/environments/{env}/head")
def branch_head(
    path: Annotated[EnvPath, Depends()],
    branch: Annotated[str, Query(min_length=1, max_length=200)],
    principal: RunReader,
    container: Container,
) -> BranchHeadResponse:
    """The current head of ``branch`` (resolved by the worker with ``git ls-remote``)."""
    container.environment_service().get(path.slug, path.env)  # 404 before asking the worker
    head = container.github.branch_head(
        path.slug, str(BranchName(branch)), principal.actor, details=True
    )
    return BranchHeadResponse.from_head(head)


@router.post("/{slug}/environments/{env}/runs", status_code=201)
def create_manual_run(
    path: Annotated[EnvPath, Depends()],
    body: ManualRunRequest,
    principal: Runner,
    container: Container,
) -> RunSummary:
    """Queue a manual run of the commit the user previewed (409 when the branch moved)."""
    run = container.run_triggers().manual(
        path.slug, path.env, body.branch, body.commit_sha, principal.run_origin()
    )
    return RunSummary.from_run(run, path.slug, path.env.value)
