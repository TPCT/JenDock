"""Pipeline schemas (see docs/api-contract.md, "Pipelines")."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from shiftcodes.domain.diagnostics import Diagnostic
from shiftcodes.domain.enums import EnvironmentName
from shiftcodes.execution.dry_run import DryPostBlock, DryRun, DryStage, DryStep, StageMode
from shiftcodes.pipeline import catalog as catalog_module
from shiftcodes.pipeline.ast import PostCondition
from shiftcodes.pipeline.errors import Severity
from shiftcodes.pipeline.model import Model
from shiftcodes.pipeline.steps import ImageRole
from shiftcodes.pipeline.templates import Template
from shiftcodes.services.pipeline_editor import DocumentSource, DryRunOutcome, PipelineDocument
from shiftcodes.services.pipelines import MAX_JENKINSFILE_BYTES, StoredPipeline

# Characters, not bytes: the service enforces the byte limit precisely.
_TEXT_LIMIT = MAX_JENKINSFILE_BYTES
Text = Field(max_length=_TEXT_LIMIT)


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DiagnosticResponse(BaseModel):
    """A finding about pipeline text; ``line`` and ``column`` are 1-based."""

    severity: Literal["error", "warning"]
    message: str
    line: int
    column: int

    @classmethod
    def from_diagnostic(cls, item: Diagnostic) -> DiagnosticResponse:
        """Convert a domain diagnostic."""
        return cls(
            severity=item.severity.value,
            message=item.message,
            line=item.span.line,
            column=item.span.column,
        )


def diagnostics_of(items: Sequence[Diagnostic]) -> list[DiagnosticResponse]:
    """Convert diagnostics, keeping their order."""
    return [DiagnosticResponse.from_diagnostic(item) for item in items]


# --- stored pipelines -----------------------------------------------------------------


class PipelineResponse(BaseModel):
    """The pipeline in force for an environment."""

    source: DocumentSource
    version: int
    jenkinsfile: str
    updated_at: datetime | None
    updated_by: str | None

    @classmethod
    def from_document(cls, document: PipelineDocument) -> PipelineResponse:
        """Convert the service result."""
        return cls(
            source=document.source,
            version=document.version,
            jenkinsfile=document.jenkinsfile,
            updated_at=document.updated_at,
            updated_by=document.updated_by,
        )


class SavePipelineRequest(_Request):
    """Body of ``PUT .../pipeline``."""

    jenkinsfile: str = Text
    base_version: int = Field(ge=0)


class VersionInfoResponse(BaseModel):
    """One entry of the version list."""

    version: int
    created_at: datetime
    created_by: str | None

    @classmethod
    def from_stored(cls, stored: StoredPipeline) -> VersionInfoResponse:
        """Convert a stored version (without its text)."""
        return cls(
            version=stored.version, created_at=stored.created_at, created_by=stored.created_by
        )


class VersionResponse(VersionInfoResponse):
    """One stored version with its text."""

    jenkinsfile: str

    @classmethod
    def from_stored(cls, stored: StoredPipeline) -> VersionResponse:
        """Convert a stored version."""
        return cls(
            version=stored.version,
            created_at=stored.created_at,
            created_by=stored.created_by,
            jenkinsfile=stored.jenkinsfile,
        )


# --- parse, render, validate ------------------------------------------------------------


class _ContextRequest(_Request):
    """Optional environment whose secret names the validator should use."""

    project: str | None = Field(default=None, max_length=64)
    environment: EnvironmentName | None = None


class ParseRequest(_ContextRequest):
    """Body of ``POST /api/pipelines/parse``."""

    jenkinsfile: str = Text


class ParseResponse(BaseModel):
    """``model`` is ``None`` only when the text has syntax errors."""

    model: Model | None
    diagnostics: list[DiagnosticResponse]


class RenderRequest(_ContextRequest):
    """Body of ``POST /api/pipelines/render``."""

    model: Model


class RenderResponse(BaseModel):
    """The canonical text of a model and the diagnostics of that text."""

    jenkinsfile: str | None
    diagnostics: list[DiagnosticResponse]


class ValidateRequest(_ContextRequest):
    """Body of ``POST /api/pipelines/validate``."""

    jenkinsfile: str = Text


class ValidateResponse(BaseModel):
    """Diagnostics only."""

    diagnostics: list[DiagnosticResponse]


# --- catalogue and templates ------------------------------------------------------------


class ArgResponse(BaseModel):
    """One argument of a step."""

    name: str
    type: catalog_module.ArgType
    required: bool
    default: str | int | bool | None
    enum: list[str] | None
    min: int | None
    max: int | None
    description: str
    multiline: bool
    role: ImageRole | None


class StepResponse(BaseModel):
    """A step, option or parameter type."""

    name: str
    title: str
    description: str
    takes_block: bool
    needs_container: bool
    args: list[ArgResponse]


class PostConditionResponse(BaseModel):
    """A post condition with its explanation."""

    name: PostCondition
    description: str


class CatalogResponse(BaseModel):
    """Everything the editor forms are built from."""

    steps: list[StepResponse]
    options: list[StepResponse]
    parameters: list[StepResponse]
    post_conditions: list[PostConditionResponse]

    @classmethod
    def from_catalog(cls, catalog: catalog_module.Catalog) -> CatalogResponse:
        """Convert the generated catalogue."""
        return cls(
            steps=[_step(item) for item in catalog.steps],
            options=[_step(item) for item in catalog.options],
            parameters=[_step(item) for item in catalog.parameters],
            post_conditions=[
                PostConditionResponse(name=item.name, description=item.description)
                for item in catalog.post_conditions
            ],
        )


def _arg(info: catalog_module.ArgInfo) -> ArgResponse:
    return ArgResponse(
        name=info.name,
        type=info.type,
        required=info.required,
        default=info.default,
        enum=None if info.choices is None else list(info.choices),
        min=info.minimum,
        max=info.maximum,
        description=info.description,
        multiline=info.multiline,
        role=info.role,
    )


def _step(info: catalog_module.StepInfo) -> StepResponse:
    return StepResponse(
        name=info.name,
        title=info.title,
        description=info.description,
        takes_block=info.takes_block,
        needs_container=info.needs_container,
        args=[_arg(arg) for arg in info.args],
    )


class TemplateResponse(BaseModel):
    """A starter pipeline."""

    name: str
    title: str
    description: str
    jenkinsfile: str

    @classmethod
    def from_template(cls, template: Template) -> TemplateResponse:
        """Convert a template."""
        return cls(
            name=template.name,
            title=template.title,
            description=template.description,
            jenkinsfile=template.jenkinsfile,
        )


# --- dry run -----------------------------------------------------------------------------


class DryRunRequest(_Request):
    """Body of ``POST .../pipeline/dry-run``: unsaved text (else the pipeline in force) and
    the branch to plan for (else the environment's own branch)."""

    jenkinsfile: str | None = Field(default=None, max_length=_TEXT_LIMIT)
    branch: str | None = Field(default=None, min_length=1, max_length=200)


class DryStepResponse(BaseModel):
    """A step of the plan; block steps contain their inner steps."""

    name: str
    label: str
    children: list[DryStepResponse]

    @classmethod
    def from_step(cls, step: DryStep) -> DryStepResponse:
        """Convert a planned step."""
        return cls(
            name=step.name,
            label=step.label,
            children=[cls.from_step(child) for child in step.children],
        )


class DryPostResponse(BaseModel):
    """A post condition and its steps."""

    condition: str
    steps: list[DryStepResponse]

    @classmethod
    def from_block(cls, block: DryPostBlock) -> DryPostResponse:
        """Convert a planned post block."""
        return cls(
            condition=block.condition, steps=[DryStepResponse.from_step(s) for s in block.steps]
        )


class DryStageResponse(BaseModel):
    """A stage of the plan with its verdict for the branch."""

    name: str
    path: str
    will_run: bool
    skip_reason: str | None
    agent: str | None
    mode: StageMode
    steps: list[DryStepResponse]
    stages: list[DryStageResponse]
    post: list[DryPostResponse]

    @classmethod
    def from_stage(cls, stage: DryStage) -> DryStageResponse:
        """Convert a planned stage."""
        return cls(
            name=stage.name,
            path=stage.path,
            will_run=stage.will_run,
            skip_reason=stage.skip_reason,
            agent=stage.agent,
            mode=stage.mode,
            steps=[DryStepResponse.from_step(s) for s in stage.steps],
            stages=[cls.from_stage(child) for child in stage.stages],
            post=[DryPostResponse.from_block(b) for b in stage.post],
        )


class DryRunPlanResponse(BaseModel):
    """The plan of a whole pipeline."""

    branch: str
    environment: str
    agent: str | None
    stages: list[DryStageResponse]
    post: list[DryPostResponse]

    @classmethod
    def from_plan(cls, plan: DryRun) -> DryRunPlanResponse:
        """Convert the plan."""
        return cls(
            branch=plan.branch,
            environment=plan.environment,
            agent=plan.agent,
            stages=[DryStageResponse.from_stage(s) for s in plan.stages],
            post=[DryPostResponse.from_block(b) for b in plan.post],
        )


class DryRunResponse(BaseModel):
    """``plan`` is ``None`` when the pipeline has errors (see ``diagnostics``)."""

    valid: bool
    diagnostics: list[DiagnosticResponse]
    plan: DryRunPlanResponse | None

    @classmethod
    def from_outcome(cls, outcome: DryRunOutcome) -> DryRunResponse:
        """Build from the dry-run service outcome."""
        valid = not any(item.severity is Severity.ERROR for item in outcome.diagnostics)
        plan = None if outcome.plan is None else DryRunPlanResponse.from_plan(outcome.plan)
        return cls(valid=valid, diagnostics=diagnostics_of(outcome.diagnostics), plan=plan)
