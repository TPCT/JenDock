"""Deployments: history, a redeploy preview and the redeploy itself.

Mirrors ``api/routers/deployments.py`` (history needs ``read``, redeploy needs ``deploy``). There is
no get-one and no rollback endpoint in the REST API, so there is no such tool: a failed deployment
is rolled back automatically by the deployer and shows as ``rolled_back`` in the history. A
redeploy is two steps like a manual run: ``preview_redeploy`` shows what would be deployed and what
runs now, ``redeploy`` queues it and refuses while a deployment of the environment is in progress.
"""

from __future__ import annotations

from shiftcodes.api.schemas.deployments import (
    DeploymentPageResponse,
    DeploymentResponse,
    RedeployResponse,
)
from shiftcodes.domain.enums import TokenScope
from shiftcodes.mcp import schema
from shiftcodes.mcp.registry import Args, Tool, ToolContext
from shiftcodes.services.deployments import MAX_PAGE_SIZE

_ENV = {"slug": schema.SLUG, "environment": schema.ENVIRONMENT}
_DEPLOYMENT = {**_ENV, "deployment_id": schema.RUN_ID}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": MAX_PAGE_SIZE, "default": 20}


def list_deployments(ctx: ToolContext, args: Args) -> object:
    """Deployments of an environment, newest first, with the images each one changed."""
    limit, offset = args.integer("limit", 20), args.integer("offset", 0)
    page = ctx.container.deployment_service().history(
        args.text("slug"), args.environment(), limit=limit, offset=offset
    )
    return DeploymentPageResponse.from_page(page, limit, offset).model_dump(mode="json")


def preview_redeploy(ctx: ToolContext, args: Args) -> object:
    """What redeploying an earlier deployment would deploy, and what runs now. Changes nothing."""
    preview = ctx.container.deployment_service().preview_redeploy(
        args.text("slug"), args.environment(), args.integer("deployment_id")
    )
    target = DeploymentResponse.from_view(preview.target)
    return {
        "deployment": target.model_dump(mode="json"),
        "would_deploy": [
            {"service": item.service, "image": item.new_image} for item in target.services
        ],
        "running_now": [
            {"service": r.service, "image": r.image, "deployment_id": r.deployment_id}
            for r in preview.running
        ],
        "in_progress": preview.in_progress,
        "can_redeploy": not preview.in_progress,
    }


def redeploy(ctx: ToolContext, args: Args) -> object:
    """Queue a run that deploys an earlier successful deployment's images again."""
    result = ctx.container.deployment_service().redeploy(
        args.text("slug"),
        args.environment(),
        args.integer("deployment_id"),
        ctx.actor,
        refuse_in_progress=True,
    )
    return RedeployResponse.from_result(result).model_dump(mode="json")


TOOLS = (
    Tool(
        "list_deployments",
        "List the deployments of an environment, newest first: status, commit, run, and for each "
        "service the previous and new image. A failed deployment is rolled back automatically "
        "(status rolled_back).",
        TokenScope.READ,
        schema.obj({**_ENV, "limit": _LIMIT, "offset": schema.OFFSET}, ["slug", "environment"]),
        list_deployments,
    ),
    Tool(
        "preview_redeploy",
        "Redeploy, step 1 of 2: show which images an earlier successful deployment would deploy "
        "again, which images run now, and whether a deployment is in progress (which blocks it). "
        "Nothing is changed. Then call redeploy with the same deployment_id.",
        TokenScope.DEPLOY,
        schema.obj(_DEPLOYMENT, ["slug", "environment", "deployment_id"]),
        preview_redeploy,
    ),
    Tool(
        "redeploy",
        "Redeploy, step 2 of 2: queue a run that deploys the images of an earlier successful "
        "deployment again without rebuilding. Refused while a deployment of the environment is "
        "in progress.",
        TokenScope.DEPLOY,
        schema.obj(_DEPLOYMENT, ["slug", "environment", "deployment_id"]),
        redeploy,
        read_only=False,
    ),
)
