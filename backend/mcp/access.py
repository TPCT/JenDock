"""Which policy action every MCP tool performs and on what its arguments point.

The dispatcher calls ``authorize_tool`` before a handler runs, so a tool cannot forget the check
and the rules are the REST rules (``backend.services.policy``). A test requires every
registered tool to be listed here. Tools that list things (``list_projects``, ``list_runs``,
``list_containers``) additionally filter their result to what the caller may see.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from backend.api import authz
from backend.api.container import AppContainer
from backend.api.principal import Principal
from backend.domain.access import GLOBAL, Action, Resource
from backend.domain.errors import ValidationError
from backend.mcp.registry import Args


class At(StrEnum):
    """What the tool's arguments identify."""

    NOTHING = "nothing"
    PROJECT = "slug"
    ENVIRONMENT = "slug + environment"
    ENVIRONMENT_OPTIONAL = "slug + environment, both or neither"
    RUN = "run_id"
    CONTAINER = "slug + environment + container_id"


@dataclass(frozen=True, slots=True)
class ToolAccess:
    """The action a tool performs; ``without`` is the action when no resource is given."""

    action: Action
    at: At = At.ENVIRONMENT
    without: Action | None = None


A = Action
TOOL_ACCESS: dict[str, ToolAccess] = {
    # projects
    "list_projects": ToolAccess(A.PROJECT_LIST, At.NOTHING),
    "get_project": ToolAccess(A.PROJECT_READ, At.PROJECT),
    "list_secret_names": ToolAccess(A.SECRET_LIST),
    # runs
    "list_runs": ToolAccess(A.RUN_READ, At.ENVIRONMENT_OPTIONAL, without=A.RUN_LIST),
    "get_run": ToolAccess(A.RUN_READ, At.RUN),
    "read_step_log": ToolAccess(A.RUN_READ, At.RUN),
    "preview_commit": ToolAccess(A.RUN_READ),
    "trigger_run": ToolAccess(A.RUN_CREATE),
    "retry_run": ToolAccess(A.RUN_RETRY, At.RUN),
    "cancel_run": ToolAccess(A.RUN_CANCEL, At.RUN),
    # pipelines
    "get_pipeline": ToolAccess(A.PIPELINE_READ),
    "list_pipeline_versions": ToolAccess(A.PIPELINE_READ),
    "get_pipeline_version": ToolAccess(A.PIPELINE_READ),
    "validate_pipeline": ToolAccess(
        A.SECRET_LIST, At.ENVIRONMENT_OPTIONAL, without=A.PIPELINE_TOOLS
    ),
    "dry_run_pipeline": ToolAccess(A.PIPELINE_READ),
    "save_pipeline": ToolAccess(A.PIPELINE_WRITE),
    "get_pipeline_catalog": ToolAccess(A.PIPELINE_TOOLS, At.NOTHING),
    "get_pipeline_templates": ToolAccess(A.PIPELINE_TOOLS, At.NOTHING),
    # deployments
    "list_deployments": ToolAccess(A.DEPLOYMENT_READ),
    "preview_redeploy": ToolAccess(A.DEPLOYMENT_REDEPLOY),
    "redeploy": ToolAccess(A.DEPLOYMENT_REDEPLOY),
    # domains
    "list_domains": ToolAccess(A.DOMAIN_READ, At.ENVIRONMENT_OPTIONAL, without=A.DOMAIN_DISCOVER),
    "discover_domains": ToolAccess(A.DOMAIN_DISCOVER_REFRESH, At.NOTHING),
    "adopt_domain": ToolAccess(A.DOMAIN_ADOPT),
    "adopt_suggested_domains": ToolAccess(A.DOMAIN_ADOPT_ALL, At.NOTHING),
    "get_domain": ToolAccess(A.DOMAIN_READ),
    "list_certificates": ToolAccess(A.CERTIFICATE_LIST, At.NOTHING),
    "check_domain": ToolAccess(A.DOMAIN_MANAGE),
    "preview_domain_change": ToolAccess(A.DOMAIN_MANAGE),
    "apply_domain": ToolAccess(A.DOMAIN_MANAGE),
    "remove_domain": ToolAccess(A.DOMAIN_MANAGE),
    "issue_certificate": ToolAccess(A.DOMAIN_MANAGE),
    # containers
    "list_containers": ToolAccess(A.CONTAINER_READ),
    "get_container": ToolAccess(A.CONTAINER_READ, At.CONTAINER),
    "container_stats": ToolAccess(A.CONTAINER_READ, At.CONTAINER),
    "host_telemetry": ToolAccess(A.TELEMETRY_READ, At.NOTHING),
    "list_container_env_names": ToolAccess(A.CONTAINER_READ, At.CONTAINER),
    "tail_container_logs": ToolAccess(A.CONTAINER_LOGS, At.CONTAINER),
    "preview_container_action": ToolAccess(A.CONTAINER_ACTION, At.CONTAINER),
    "confirm_container_action": ToolAccess(A.CONTAINER_ACTION, At.CONTAINER),
    "update_container_limits": ToolAccess(A.CONTAINER_LIMITS, At.CONTAINER),
    "get_resource_limits": ToolAccess(A.CONTAINER_READ),
    "get_resource_usage": ToolAccess(A.CONTAINER_READ),
    "set_resource_limits": ToolAccess(A.CONTAINER_LIMITS),
    "remove_resource_limits": ToolAccess(A.CONTAINER_LIMITS),
    # databases
    "list_database_connections": ToolAccess(A.DATABASE_READ),
    "get_database_connection": ToolAccess(A.DATABASE_READ),
    "list_database_schemas": ToolAccess(A.DATABASE_READ),
    "list_database_tables": ToolAccess(A.DATABASE_READ),
    "get_database_table": ToolAccess(A.DATABASE_READ),
    "read_table_rows": ToolAccess(A.DATABASE_DATA),
    "run_readonly_query": ToolAccess(A.DATABASE_DATA),
}


def action_of(tool_name: str) -> Action | None:
    """The action a tool performs (``None`` for an unknown tool)."""
    access = TOOL_ACCESS.get(tool_name)
    return access.action if access else None


def authorize_tool(
    container: AppContainer, principal: Principal, tool_name: str, args: Args
) -> None:
    """Raise unless the principal may call the tool with these arguments."""
    access = TOOL_ACCESS.get(tool_name)
    if access is None:  # a tool without a rule is never callable
        raise ValidationError(f"tool has no access rule: {tool_name}")
    if access.at is At.NOTHING:
        authz.authorize(principal, access.action, GLOBAL)
    elif access.at is At.PROJECT:
        authz.authorize(principal, access.action, Resource.of_project(args.text("slug")))
    elif access.at is At.RUN:
        authz.authorize_run(container, principal, access.action, args.integer("run_id"))
    elif access.at is At.CONTAINER:
        authz.authorize_container(
            container,
            principal,
            access.action,
            args.text("slug"),
            args.environment(),
            args.text("container_id"),
        )
    elif access.at is At.ENVIRONMENT_OPTIONAL:
        _authorize_optional(principal, access, args)
    else:
        resource = Resource.of_environment(args.text("slug"), args.environment())
        authz.authorize(principal, access.action, resource)


def _authorize_optional(principal: Principal, access: ToolAccess, args: Args) -> None:
    slug, env = args.optional_text("slug"), args.optional_environment()
    if (slug is None) != (env is None):
        raise ValidationError("give both slug and environment, or neither")
    if slug is not None and env is not None:
        authz.authorize(principal, access.action, Resource.of_environment(slug, env))
    elif access.without is not None:
        authz.authorize(principal, access.without, GLOBAL)
