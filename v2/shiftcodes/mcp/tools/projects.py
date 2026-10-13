"""Projects, environments and secret names (never values)."""

from __future__ import annotations

from shiftcodes.api.schemas.projects import ProjectResponse
from shiftcodes.api.schemas.secrets import SecretResponse
from shiftcodes.domain.enums import TokenScope
from shiftcodes.mcp import schema
from shiftcodes.mcp.registry import Args, Tool, ToolContext


def list_projects(ctx: ToolContext, _args: Args) -> object:
    """Every project with its two environments (branch, auto-deploy, latest run)."""
    views = ctx.container.project_queries().list_projects()
    return [ProjectResponse.from_view(view).model_dump(mode="json") for view in views]


def get_project(ctx: ToolContext, args: Args) -> object:
    """One project with its environments."""
    view = ctx.container.project_queries().get(args.text("slug"))
    return ProjectResponse.from_view(view).model_dump(mode="json")


def list_secret_names(ctx: ToolContext, args: Args) -> object:
    """Names and timestamps of the environment's secrets. Values are never available here."""
    infos = ctx.container.secrets.list_secrets(args.text("slug"), args.environment(), ctx.actor)
    return [SecretResponse.from_info(info).model_dump(mode="json") for info in infos]


TOOLS = (
    Tool(
        "list_projects",
        "List the projects with their development and production environments: tracked branch, "
        "auto-deploy flag and the latest run of each.",
        TokenScope.READ,
        schema.obj(),
        list_projects,
    ),
    Tool(
        "get_project",
        "Get one project (repository, git transport, environments and their latest runs).",
        TokenScope.READ,
        schema.obj({"slug": schema.SLUG}, ["slug"]),
        get_project,
    ),
    Tool(
        "list_secret_names",
        "List the names of an environment's secrets (credentials a pipeline can use through "
        "credentials('NAME')). Values cannot be read through MCP.",
        TokenScope.READ,
        schema.obj(
            {"slug": schema.SLUG, "environment": schema.ENVIRONMENT}, ["slug", "environment"]
        ),
        list_secret_names,
    ),
)
