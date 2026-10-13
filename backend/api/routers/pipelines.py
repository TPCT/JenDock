"""Pipeline endpoints: the stored pipeline of an environment and the stateless editor helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends

from backend.api.deps import Container, require_scope
from backend.api.principal import Principal
from backend.api.schemas.pipelines import (
    CatalogResponse,
    DryRunRequest,
    DryRunResponse,
    ParseRequest,
    ParseResponse,
    PipelineResponse,
    RenderRequest,
    RenderResponse,
    SavePipelineRequest,
    TemplateResponse,
    ValidateRequest,
    ValidateResponse,
    VersionInfoResponse,
    VersionResponse,
    diagnostics_of,
)
from backend.domain.enums import EnvironmentName, TokenScope
from backend.pipeline.catalog import build_catalog
from backend.pipeline.templates import load_templates

environment_router = APIRouter(
    prefix="/api/projects/{slug}/environments/{env}/pipeline", tags=["pipelines"]
)
editor_router = APIRouter(prefix="/api/pipelines", tags=["pipelines"])
Reader = Annotated[Principal, Depends(require_scope(TokenScope.READ))]
Writer = Annotated[Principal, Depends(require_scope(TokenScope.PIPELINES))]


@dataclass(frozen=True, slots=True)
class EnvPath:
    """Path parameters identifying an environment."""

    slug: str
    env: EnvironmentName


@dataclass(frozen=True, slots=True)
class VersionPath:
    """Path parameters identifying one stored version."""

    slug: str
    env: EnvironmentName
    version: int


@environment_router.get("")
def get_pipeline(
    path: Annotated[EnvPath, Depends()], principal: Reader, container: Container
) -> PipelineResponse:
    """The pipeline in force: the repository's Jenkinsfile, else the stored one, else none."""
    document = container.pipeline_documents().document(path.slug, path.env, principal.actor)
    return PipelineResponse.from_document(document)


@environment_router.put("")
def put_pipeline(
    path: Annotated[EnvPath, Depends()],
    body: SavePipelineRequest,
    principal: Writer,
    container: Container,
) -> PipelineResponse:
    """Store a new version; 409 when ``base_version`` is stale or the repository owns the file."""
    document = container.pipeline_documents().save(
        path.slug, path.env, body.jenkinsfile, body.base_version, principal.actor
    )
    return PipelineResponse.from_document(document)


@environment_router.get("/versions")
def list_versions(
    path: Annotated[EnvPath, Depends()], _principal: Reader, container: Container
) -> list[VersionInfoResponse]:
    """Stored versions, newest first (without their text)."""
    history = container.pipeline_documents().history(path.slug, path.env)
    return [VersionInfoResponse.from_stored(item) for item in history]


@environment_router.get("/versions/{version}")
def get_version(
    path: Annotated[VersionPath, Depends()], _principal: Reader, container: Container
) -> VersionResponse:
    """One stored version with its text."""
    stored = container.pipeline_documents().version(path.slug, path.env, path.version)
    return VersionResponse.from_stored(stored)


@environment_router.post("/dry-run")
def dry_run(
    path: Annotated[EnvPath, Depends()],
    body: DryRunRequest,
    principal: Reader,
    container: Container,
) -> DryRunResponse:
    """What the pipeline would do on a branch, without running anything."""
    outcome = container.pipeline_documents().dry_run(
        path.slug, path.env, body.jenkinsfile, body.branch, principal.actor
    )
    return DryRunResponse.from_outcome(outcome)


@editor_router.get("/catalog")
def get_catalog(_principal: Reader) -> CatalogResponse:
    """Step definitions for the editor forms, generated from the engine's own registries."""
    return CatalogResponse.from_catalog(build_catalog())


@editor_router.get("/templates")
def get_templates(_principal: Reader) -> list[TemplateResponse]:
    """Starter pipelines for common project types."""
    return [TemplateResponse.from_template(item) for item in load_templates()]


@editor_router.post("/parse")
def parse_pipeline(body: ParseRequest, _principal: Reader, container: Container) -> ParseResponse:
    """Text to editing model; ``model`` is ``null`` only after a syntax error."""
    editor = container.pipeline_editor()
    outcome = editor.parse(body.jenkinsfile, editor.context(body.project, body.environment))
    return ParseResponse(model=outcome.model, diagnostics=diagnostics_of(outcome.diagnostics))


@editor_router.post("/render")
def render_pipeline(
    body: RenderRequest, _principal: Reader, container: Container
) -> RenderResponse:
    """Editing model to canonical text; half-filled models render and are diagnosed."""
    editor = container.pipeline_editor()
    rendering = editor.render(body.model, editor.context(body.project, body.environment))
    return RenderResponse(
        jenkinsfile=rendering.jenkinsfile, diagnostics=diagnostics_of(rendering.diagnostics)
    )


@editor_router.post("/validate")
def validate_pipeline(
    body: ValidateRequest, _principal: Reader, container: Container
) -> ValidateResponse:
    """Diagnostics of Jenkinsfile text."""
    editor = container.pipeline_editor()
    found = editor.validate(body.jenkinsfile, editor.context(body.project, body.environment))
    return ValidateResponse(diagnostics=diagnostics_of(found))
