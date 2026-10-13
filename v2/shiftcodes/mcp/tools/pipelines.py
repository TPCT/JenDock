"""Pipelines: read, validate, dry-run, save, and the step catalogue and templates."""

from __future__ import annotations

from shiftcodes.api.schemas.pipelines import (
    CatalogResponse,
    DryRunResponse,
    PipelineResponse,
    TemplateResponse,
    ValidateResponse,
    VersionInfoResponse,
    VersionResponse,
    diagnostics_of,
)
from shiftcodes.domain.enums import TokenScope
from shiftcodes.mcp import schema
from shiftcodes.mcp.registry import Args, Tool, ToolContext
from shiftcodes.pipeline.catalog import build_catalog
from shiftcodes.pipeline.templates import load_templates

_TARGET = {"slug": schema.SLUG, "environment": schema.ENVIRONMENT}
_REQUIRED_TARGET = ["slug", "environment"]


def get_pipeline(ctx: ToolContext, args: Args) -> object:
    """The pipeline in force (repository Jenkinsfile, else the stored version, else none)."""
    document = ctx.container.pipeline_documents().document(
        args.text("slug"), args.environment(), ctx.actor
    )
    return PipelineResponse.from_document(document).model_dump(mode="json")


def list_pipeline_versions(ctx: ToolContext, args: Args) -> object:
    """Stored versions, newest first, without their text."""
    history = ctx.container.pipeline_documents().history(args.text("slug"), args.environment())
    return [VersionInfoResponse.from_stored(item).model_dump(mode="json") for item in history]


def get_pipeline_version(ctx: ToolContext, args: Args) -> object:
    """One stored version with its text."""
    stored = ctx.container.pipeline_documents().version(
        args.text("slug"), args.environment(), args.integer("version")
    )
    return VersionResponse.from_stored(stored).model_dump(mode="json")


def validate_pipeline(ctx: ToolContext, args: Args) -> object:
    """Diagnostics of Jenkinsfile text (syntax and semantics for the environment)."""
    editor = ctx.container.pipeline_editor()
    context = editor.context(args.optional_text("slug"), args.optional_environment())
    found = editor.validate(args.text("jenkinsfile"), context)
    return ValidateResponse(diagnostics=diagnostics_of(found)).model_dump(mode="json")


def dry_run_pipeline(ctx: ToolContext, args: Args) -> object:
    """What the pipeline (given text, else the one in force) would do on a branch."""
    outcome = ctx.container.pipeline_documents().dry_run(
        args.text("slug"),
        args.environment(),
        args.optional_text("jenkinsfile"),
        args.optional_text("branch"),
        ctx.actor,
    )
    return DryRunResponse.from_outcome(outcome).model_dump(mode="json")


def save_pipeline(ctx: ToolContext, args: Args) -> object:
    """Store a new pipeline version (validated; conflict when base_version is stale)."""
    document = ctx.container.pipeline_documents().save(
        args.text("slug"),
        args.environment(),
        args.text("jenkinsfile"),
        args.integer("base_version"),
        ctx.actor,
    )
    return PipelineResponse.from_document(document).model_dump(mode="json")


def get_catalog(_ctx: ToolContext, _args: Args) -> object:
    """The steps, options, parameters and post conditions the Jenkinsfile engine supports."""
    return CatalogResponse.from_catalog(build_catalog()).model_dump(mode="json")


def get_templates(_ctx: ToolContext, _args: Args) -> object:
    """Starter pipelines for common project types."""
    return [TemplateResponse.from_template(t).model_dump(mode="json") for t in load_templates()]


TOOLS = (
    Tool(
        "get_pipeline",
        "Get the pipeline of an environment: source (repository, platform or none), version and "
        "Jenkinsfile text. A Jenkinsfile in the repository wins and cannot be saved over.",
        TokenScope.READ,
        schema.obj(_TARGET, _REQUIRED_TARGET),
        get_pipeline,
    ),
    Tool(
        "list_pipeline_versions",
        "List the stored pipeline versions of an environment, newest first.",
        TokenScope.READ,
        schema.obj(_TARGET, _REQUIRED_TARGET),
        list_pipeline_versions,
    ),
    Tool(
        "get_pipeline_version",
        "Get one stored pipeline version with its text.",
        TokenScope.READ,
        schema.obj(
            {**_TARGET, "version": {"type": "integer", "minimum": 1}},
            [*_REQUIRED_TARGET, "version"],
        ),
        get_pipeline_version,
    ),
    Tool(
        "validate_pipeline",
        "Validate Jenkinsfile text. Pass slug and environment to check credentials('NAME') "
        "against that environment's secrets. Returns diagnostics with line and column.",
        TokenScope.READ,
        schema.obj({"jenkinsfile": schema.JENKINSFILE, **_TARGET}, ["jenkinsfile"]),
        validate_pipeline,
        redact=frozenset({"jenkinsfile"}),
    ),
    Tool(
        "dry_run_pipeline",
        "Show what a pipeline would do on a branch (stages, which run or are skipped) without "
        "running anything. Omit jenkinsfile to test the one in force.",
        TokenScope.READ,
        schema.obj(
            {**_TARGET, "jenkinsfile": schema.JENKINSFILE, "branch": schema.BRANCH},
            _REQUIRED_TARGET,
        ),
        dry_run_pipeline,
        redact=frozenset({"jenkinsfile"}),
    ),
    Tool(
        "save_pipeline",
        "Store a new version of the environment's pipeline. base_version must be the version "
        "you read (0 when none exists); otherwise the save is refused with a conflict. The text "
        "is validated first and errors are returned as diagnostics.",
        TokenScope.PIPELINES,
        schema.obj(
            {
                **_TARGET,
                "jenkinsfile": schema.JENKINSFILE,
                "base_version": {"type": "integer", "minimum": 0},
            },
            [*_REQUIRED_TARGET, "jenkinsfile", "base_version"],
        ),
        save_pipeline,
        read_only=False,
        redact=frozenset({"jenkinsfile"}),
    ),
    Tool(
        "get_pipeline_catalog",
        "The Jenkinsfile subset this platform supports: every step with its arguments, options, "
        "parameters and post conditions.",
        TokenScope.READ,
        schema.obj(),
        get_catalog,
    ),
    Tool(
        "get_pipeline_templates",
        "Starter pipelines (Jenkinsfile text) for common project types.",
        TokenScope.READ,
        schema.obj(),
        get_templates,
    ),
)
