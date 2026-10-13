"""Projects, environments and secret names (never values)."""

from __future__ import annotations

from backend.api import authz
from backend.api.schemas.projects import ProjectPage, ProjectResponse
from backend.api.schemas.secrets import SecretResponse
from backend.domain.enums import TokenScope
from backend.mcp import schema
from backend.mcp.registry import Args, Tool, ToolContext

MCP_PROJECTS_PER_PAGE = 100  # the tool used to return every project; keep that for most setups


def list_projects(ctx: ToolContext, args: Args) -> object:
    """A page of the projects the token may see, with their two environments."""
    only = authz.project_filter(ctx.principal)
    page = ctx.container.project_queries().search_projects(
        args.page_query(MCP_PROJECTS_PER_PAGE), only
    )
    return ProjectPage.from_page(page).model_dump(mode="json")


def get_project(ctx: ToolContext, args: Args) -> object:
    """One project with its environments."""
    only = authz.project_filter(ctx.principal)
    view = ctx.container.project_queries().get(args.text("slug"), only)
    return ProjectResponse.from_view(view).model_dump(mode="json")


def list_secret_names(ctx: ToolContext, args: Args) -> object:
    """Names and timestamps of the environment's secrets. Values are never available here."""
    infos = ctx.container.secrets.list_secrets(args.text("slug"), args.environment(), ctx.actor)
    return [SecretResponse.from_info(info).model_dump(mode="json") for info in infos]


TOOLS = (
    Tool(
        "list_projects",
        "List the projects with their development and production environments: tracked branch, "
        "auto-deploy flag and the latest run of each. Paged (up to 100 per page): page, "
        "per_page, q (name, slug or repository), sort (name, slug, created).",
        TokenScope.READ,
        schema.obj(schema.paging(("name", "slug", "created"), MCP_PROJECTS_PER_PAGE)),
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
