"""Environment power controls; mirrors the environment power REST routes."""

from __future__ import annotations

from backend.domain.enums import EnvironmentName, TokenScope
from backend.domain.errors import ValidationError
from backend.mcp import schema
from backend.mcp.registry import Args, Tool, ToolContext

_ENV = {"slug": schema.SLUG, "environment": schema.ENVIRONMENT}


def stop_environment(ctx: ToolContext, args: Args) -> object:
    slug = args.text("slug")
    environment = args.environment()
    confirmation = args.optional_text("confirm")
    if environment is EnvironmentName.PRODUCTION and confirmation != slug:
        raise ValidationError(f"Type the project slug '{slug}' to stop production.")
    result = ctx.container.container_gateway().stop_environment(slug, environment, ctx.actor)
    return result.model_dump(mode="json")


def start_environment(ctx: ToolContext, args: Args) -> object:
    return (
        ctx.container.container_gateway()
        .start_environment(args.text("slug"), args.environment(), ctx.actor)
        .model_dump(mode="json")
    )


TOOLS = (
    Tool(
        "stop_environment",
        "Stop every service while preserving containers and volumes. Production requires confirm "
        "to equal the project slug.",
        TokenScope.DEPLOY,
        schema.obj({**_ENV, "confirm": {"type": "string"}}, ["slug", "environment"]),
        stop_environment,
        read_only=False,
    ),
    Tool(
        "start_environment",
        "Start every service from the last deployed images without building or pulling.",
        TokenScope.DEPLOY,
        schema.obj(_ENV, ["slug", "environment"]),
        start_environment,
        read_only=False,
    ),
)
