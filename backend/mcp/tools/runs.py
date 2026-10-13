"""Runs: list, inspect, read logs, and start, retry or cancel them."""

from __future__ import annotations

from backend.api.schemas.projects import BranchHeadResponse
from backend.api.schemas.runs import CancelResponse, RunPage, RunResponse, RunSummary
from backend.domain.enums import TokenScope
from backend.domain.errors import ValidationError
from backend.domain.values import BranchName
from backend.mcp import schema
from backend.mcp.registry import Args, Tool, ToolContext


def list_runs(ctx: ToolContext, args: Args) -> object:
    """A page of runs, newest first: all runs, or those of one environment."""
    queries = ctx.container.project_queries()
    limit, offset = args.integer("limit", 20), args.integer("offset", 0)
    slug, env = args.optional_text("slug"), args.optional_environment()
    if (slug is None) != (env is None):
        raise ValidationError("give both slug and environment, or neither")
    if slug is not None and env is not None:
        page = queries.environment_runs(slug, env, limit=limit, offset=offset)
    else:
        page = queries.all_runs(limit=limit, offset=offset)
    return RunPage.from_page(page).model_dump(mode="json")


def get_run(ctx: ToolContext, args: Args) -> object:
    """A run with its stages and steps."""
    detail = ctx.container.run_queries().detail(args.integer("run_id"))
    return RunResponse.from_detail(detail).model_dump(mode="json")


def read_step_log(ctx: ToolContext, args: Args) -> object:
    """A bounded chunk of a step log (already secret-masked); continue with next_offset."""
    run_id, step_id = args.integer("run_id"), args.integer("step_id")
    return ctx.container.step_log(run_id, step_id, args.integer("offset", 0)).model_dump(
        mode="json"
    )


def preview_commit(ctx: ToolContext, args: Args) -> object:
    """Step one of a manual run: the current head commit of a branch."""
    slug, env = args.text("slug"), args.environment()
    ctx.container.environment_service().get(slug, env)
    branch = str(BranchName(args.text("branch")))
    head = ctx.container.github.branch_head(slug, branch, ctx.actor, details=True)
    return BranchHeadResponse.from_head(head).model_dump(mode="json")


def trigger_run(ctx: ToolContext, args: Args) -> object:
    """Step two: queue a run of exactly the previewed commit (conflict if the branch moved)."""
    slug, env = args.text("slug"), args.environment()
    run = ctx.container.run_triggers().manual(
        slug, env, args.text("branch"), args.text("commit_sha"), ctx.origin()
    )
    return RunSummary.from_run(run, slug, env.value).model_dump(mode="json")


def retry_run(ctx: ToolContext, args: Args) -> object:
    """Queue a new run of the same commit and branch as a finished run."""
    run = ctx.container.run_triggers().retry(args.integer("run_id"), ctx.origin())
    detail = ctx.container.run_queries().detail(run.id)
    return RunSummary.from_run(run, detail.project, detail.environment).model_dump(mode="json")


def cancel_run(ctx: ToolContext, args: Args) -> object:
    """Cancel a queued run now, or ask the worker to stop a running one."""
    run_id = args.integer("run_id")
    outcome = ctx.container.run_lifecycle().request_cancel(run_id, ctx.actor)
    detail = ctx.container.run_queries().detail(run_id)
    response = CancelResponse(outcome=outcome, run=RunResponse.from_detail(detail))
    return response.model_dump(mode="json")


_TARGET = {"slug": schema.SLUG, "environment": schema.ENVIRONMENT}

TOOLS = (
    Tool(
        "list_runs",
        "List runs, newest first. Without slug/environment: every project; with both: one "
        "environment.",
        TokenScope.READ,
        schema.obj(
            {
                "slug": schema.SLUG,
                "environment": schema.ENVIRONMENT,
                "limit": schema.LIMIT,
                "offset": schema.OFFSET,
            }
        ),
        list_runs,
    ),
    Tool(
        "get_run",
        "Get a run: status, trigger, commit, pipeline source and its stages and steps (the step "
        "ids are what read_step_log needs).",
        TokenScope.READ,
        schema.obj({"run_id": schema.RUN_ID}, ["run_id"]),
        get_run,
    ),
    Tool(
        "read_step_log",
        "Read a step's log from a byte offset (at most 64 KiB, cut at a line). Continue with "
        "offset=next_offset until complete is true. Secrets are masked.",
        TokenScope.READ,
        schema.obj(
            {
                "run_id": schema.RUN_ID,
                "step_id": schema.RUN_ID,
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
            ["run_id", "step_id"],
        ),
        read_step_log,
    ),
    Tool(
        "preview_commit",
        "Manual run, step 1 of 2: show the current head commit (sha, message, author, date) of "
        "a branch so the user can confirm it. Then call trigger_run with that exact commit_sha.",
        TokenScope.READ,
        schema.obj({**_TARGET, "branch": schema.BRANCH}, ["slug", "environment", "branch"]),
        preview_commit,
    ),
    Tool(
        "trigger_run",
        "Manual run, step 2 of 2: queue a run of the commit returned by preview_commit. Fails "
        "with a conflict when the branch head moved since, so look at the new head first.",
        TokenScope.RUN,
        schema.obj(
            {**_TARGET, "branch": schema.BRANCH, "commit_sha": schema.COMMIT_SHA},
            ["slug", "environment", "branch", "commit_sha"],
        ),
        trigger_run,
        read_only=False,
    ),
    Tool(
        "retry_run",
        "Queue a new run with the same commit and branch as a finished run (trigger: manual).",
        TokenScope.RUN,
        schema.obj({"run_id": schema.RUN_ID}, ["run_id"]),
        retry_run,
        read_only=False,
    ),
    Tool(
        "cancel_run",
        "Cancel a queued run immediately, or request cancellation of a running one (it stops "
        "between steps). A finished run cannot be cancelled.",
        TokenScope.RUN,
        schema.obj({"run_id": schema.RUN_ID}, ["run_id"]),
        cancel_run,
        read_only=False,
    ),
)
