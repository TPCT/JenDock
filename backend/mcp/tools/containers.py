"""Containers: inventory, stats, host telemetry, bounded logs, lifecycle actions and limits.

Resource limits per compose service (``get_resource_limits``, ``get_resource_usage``,
``set_resource_limits``, ``remove_resource_limits``) mirror ``api/routers/resources.py``: reading
needs ``read``, changing needs ``containers`` and a maintainer on the environment (the role a
deploy needs, production included).

Scopes mirror ``api/routers/containers.py`` and ``telemetry.py``: inventory, detail, stats, env
variable names and host telemetry need ``read``; logs, lifecycle and limits need ``containers``
(as in REST, where reading logs is a ``containers`` operation). Start, stop and restart are two
steps with the worker's single-use confirmation.

Deliberate gaps (agents must not read or change secrets or open a shell): there is no terminal, no
log follow stream, no env value reveal, no env edit and no container file browsing over MCP.
"""

from __future__ import annotations

from collections.abc import Mapping

from backend.api import authz
from backend.api.schemas.containers import ActionRequest, ConfirmRequest, LimitsRequest
from backend.containers.logs import DEFAULT_TAIL, MAX_TAIL, LogCursor
from backend.domain.containers import LifecycleAction, validate_container_id
from backend.domain.enums import TokenScope
from backend.domain.resources import ResourceLimits, parse_memory
from backend.mcp import schema
from backend.mcp.registry import Args, JsonSchema, Tool, ToolContext

_ENV = {"slug": schema.SLUG, "environment": schema.ENVIRONMENT}
_CONTAINER_ID: JsonSchema = {
    "type": "string",
    "pattern": "^[0-9a-f]{64}$",
    "description": "the full 64 character container id from list_containers",
}
_TARGET = {**_ENV, "container_id": _CONTAINER_ID}
_REQUIRED_ENV = ["slug", "environment"]
_REQUIRED_TARGET = [*_REQUIRED_ENV, "container_id"]


def _container_id(args: Args) -> str:
    return validate_container_id(args.text("container_id"))


def list_containers(ctx: ToolContext, args: Args) -> object:
    """The containers of the environment's compose project."""
    gateway = ctx.container.container_gateway()
    slug, env = args.text("slug"), args.environment()
    views = gateway.list_containers(slug, env, ctx.actor)
    visible = authz.visible_containers(ctx.principal, slug, env, views)
    return [view.model_dump(mode="json") for view in visible]


def get_container(ctx: ToolContext, args: Args) -> object:
    """One container by its exact id."""
    gateway = ctx.container.container_gateway()
    view = gateway.get_container(
        args.text("slug"), args.environment(), _container_id(args), ctx.actor
    )
    return view.model_dump(mode="json")


def container_stats(ctx: ToolContext, args: Args) -> object:
    """The latest ``docker stats`` sample and the recent history of a container."""
    gateway = ctx.container.container_gateway()
    view = gateway.container_stats(
        args.text("slug"), args.environment(), _container_id(args), ctx.actor
    )
    return view.model_dump(mode="json")


def host_telemetry(ctx: ToolContext, _args: Args) -> object:
    """CPU, RAM and disk of the host with a short history."""
    return ctx.container.container_gateway().host_telemetry(ctx.actor).model_dump(mode="json")


def list_env_names(ctx: ToolContext, args: Args) -> object:
    """The container's environment variable names (no values, not even masked ones)."""
    gateway = ctx.container.container_gateway()
    items = gateway.env_list(args.text("slug"), args.environment(), _container_id(args), ctx.actor)
    return [
        {"name": item.name, "sensitive": item.sensitive, "overridden": item.overridden}
        for item in items
    ]


def tail_logs(ctx: ToolContext, args: Args) -> object:
    """The last lines of a container log (masked), or those after a cursor. No follow."""
    since, skip = args.optional_text("since"), args.integer("skip", 0)
    cursor = LogCursor(since, skip) if since else None
    gateway = ctx.container.container_gateway()
    page = gateway.logs(
        args.text("slug"),
        args.environment(),
        _container_id(args),
        ctx.actor,
        tail=args.integer("tail", DEFAULT_TAIL),
        cursor=cursor,
    )
    return page.model_dump(mode="json")


def preview_action(ctx: ToolContext, args: Args) -> object:
    """Step 1: what start, stop or restart would do, with the single-use confirmation."""
    action = ActionRequest(action=LifecycleAction(args.text("action"))).action
    gateway = ctx.container.container_gateway()
    plan = gateway.plan_action(
        args.text("slug"), args.environment(), _container_id(args), action, ctx.actor
    )
    return plan.model_dump(mode="json")


def confirm_action(ctx: ToolContext, args: Args) -> object:
    """Step 2: run the planned action on exactly that container."""
    confirmation = ConfirmRequest(confirmation=args.text("confirmation")).confirmation
    gateway = ctx.container.container_gateway()
    result = gateway.confirm_action(
        args.text("slug"), args.environment(), _container_id(args), confirmation, ctx.actor
    )
    return result.model_dump(mode="json")


def update_limits(ctx: ToolContext, args: Args) -> object:
    """Set CPU and/or memory limits (within the configured maxima)."""
    values: Mapping[str, object] = args.values
    cpus = values.get("cpus")
    request = LimitsRequest(
        cpus=float(cpus) if isinstance(cpus, int | float) and not isinstance(cpus, bool) else None,
        memory=args.optional_text("memory"),
    )
    gateway = ctx.container.container_gateway()
    view = gateway.update_limits(
        args.text("slug"),
        args.environment(),
        _container_id(args),
        request.cpus,
        request.memory,
        ctx.actor,
    )
    return view.model_dump(mode="json")


def _service(args: Args) -> str:
    return args.text("service")


def get_resource_limits(ctx: ToolContext, args: Args) -> object:
    """The saved CPU/memory limits of the environment's services and the host's capacity."""
    slug, env = args.text("slug"), args.environment()
    overview = ctx.container.container_gateway().resources_list(slug, env, ctx.actor)
    allowed = set(
        authz.visible_services(
            ctx.principal, slug, env, [item.service for item in overview.services]
        )
    )
    services = [item for item in overview.services if item.service in allowed]
    return overview.model_copy(update={"services": tuple(services)}).model_dump(mode="json")


def get_resource_usage(ctx: ToolContext, args: Args) -> object:
    """Live CPU and memory use of the running containers (``docker stats --no-stream``)."""
    slug, env = args.text("slug"), args.environment()
    usage = ctx.container.container_gateway().resources_usage(slug, env, ctx.actor)
    names = [item.service or "" for item in usage.containers]
    allowed = set(authz.visible_services(ctx.principal, slug, env, names))
    containers = [item for item in usage.containers if (item.service or "") in allowed]
    return usage.model_copy(update={"containers": tuple(containers)}).model_dump(mode="json")


def _number(args: Args, name: str) -> float | None:
    value = args.values.get(name)
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


def _memory(args: Args, name: str) -> int | None:
    text = args.optional_text(name)
    return parse_memory(text) if text is not None else None


def set_resource_limits(ctx: ToolContext, args: Args) -> object:
    """Save the complete set of limits of a service; optionally apply them live."""
    shares = _number(args, "cpu_shares")
    limits = ResourceLimits(
        cpus=_number(args, "cpus"),
        memory_bytes=_memory(args, "memory"),
        memory_reservation_bytes=_memory(args, "memory_reservation"),
        cpu_shares=int(shares) if shares is not None else None,
    )
    apply_now = args.values.get("apply_now") is True
    change = ctx.container.container_gateway().resources_save(
        args.text("slug"), args.environment(), _service(args), limits, apply_now, ctx.actor
    )
    return change.model_dump(mode="json")


def remove_resource_limits(ctx: ToolContext, args: Args) -> object:
    """Remove the saved limits of a service; optionally lift them on the running containers."""
    apply_now = args.values.get("apply_now") is True
    change = ctx.container.container_gateway().resources_remove(
        args.text("slug"), args.environment(), _service(args), apply_now, ctx.actor
    )
    return change.model_dump(mode="json")


_SERVICE: JsonSchema = {
    "type": "string",
    "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$",
    "description": "the compose service name",
}
_APPLY_NOW: JsonSchema = {
    "type": "boolean",
    "description": "also update the running containers now with docker update (no restart)",
}

TOOLS = (
    Tool(
        "list_containers",
        "List the containers of an environment's compose project: state, health, image, "
        "revision, ports, mounts and limits. Use the full id in the other container tools.",
        TokenScope.READ,
        schema.obj(_ENV, _REQUIRED_ENV),
        list_containers,
    ),
    Tool(
        "get_container",
        "Get one container by its full id.",
        TokenScope.READ,
        schema.obj(_TARGET, _REQUIRED_TARGET),
        get_container,
    ),
    Tool(
        "container_stats",
        "The latest CPU/memory/network/IO sample of a container and its recent history.",
        TokenScope.READ,
        schema.obj(_TARGET, _REQUIRED_TARGET),
        container_stats,
    ),
    Tool(
        "host_telemetry",
        "CPU, RAM and disk of the host as the worker sees them, with a short history.",
        TokenScope.READ,
        schema.obj(),
        host_telemetry,
    ),
    Tool(
        "list_container_env_names",
        "List the names of a container's environment variables with whether each looks "
        "sensitive and whether it is overridden. Values cannot be read or changed over MCP.",
        TokenScope.READ,
        schema.obj(_TARGET, _REQUIRED_TARGET),
        list_env_names,
    ),
    Tool(
        "tail_container_logs",
        "Read the last lines of a container's log (secrets masked), at most 1000. To continue, "
        "pass the returned cursor's since and skip. There is no live follow over MCP.",
        TokenScope.CONTAINERS,
        schema.obj(
            {
                **_TARGET,
                "tail": {"type": "integer", "minimum": 1, "maximum": MAX_TAIL},
                "since": {"type": "string", "maxLength": 40},
                "skip": {"type": "integer", "minimum": 0, "maximum": 100_000},
            },
            _REQUIRED_TARGET,
        ),
        tail_logs,
    ),
    Tool(
        "preview_container_action",
        "Start, stop or restart, step 1 of 2: show what the action would do to this container "
        "and return a single-use confirmation. Nothing happens yet. Ask the user to confirm, "
        "then call confirm_container_action.",
        TokenScope.CONTAINERS,
        schema.obj(
            {**_TARGET, "action": {"type": "string", "enum": [a.value for a in LifecycleAction]}},
            [*_REQUIRED_TARGET, "action"],
        ),
        preview_action,
        read_only=False,
    ),
    Tool(
        "confirm_container_action",
        "Start, stop or restart, step 2 of 2: execute the planned action on exactly that "
        "container using the confirmation from preview_container_action.",
        TokenScope.CONTAINERS,
        schema.obj(
            {**_TARGET, "confirmation": {"type": "string", "minLength": 1, "maxLength": 200}},
            [*_REQUIRED_TARGET, "confirmation"],
        ),
        confirm_action,
        read_only=False,
        redact=frozenset({"confirmation"}),
    ),
    Tool(
        "update_container_limits",
        "Set CPU and/or memory limits of a container (within the configured maxima); the "
        "override is persisted. Give at least one of cpus and memory (for example 512m).",
        TokenScope.CONTAINERS,
        schema.obj(
            {
                **_TARGET,
                "cpus": {"type": "number", "exclusiveMinimum": 0, "maximum": 1024},
                "memory": {"type": "string", "minLength": 1, "maxLength": 32},
            },
            _REQUIRED_TARGET,
        ),
        update_limits,
        read_only=False,
    ),
    Tool(
        "get_resource_limits",
        "The saved CPU and memory limits of every service of an environment (cpus, memory, "
        "memory reservation and cpu shares; null when a service has none) with the host's CPUs "
        "and memory. Limits are saved for future deploys; set_resource_limits can also apply "
        "them to the running containers.",
        TokenScope.READ,
        schema.obj(_ENV, _REQUIRED_ENV),
        get_resource_limits,
    ),
    Tool(
        "get_resource_usage",
        "Live CPU and memory use of the running containers of an environment against their "
        "applied limits, with the host's CPUs and memory (a docker stats --no-stream).",
        TokenScope.READ,
        schema.obj(_ENV, _REQUIRED_ENV),
        get_resource_usage,
    ),
    Tool(
        "set_resource_limits",
        "Save the complete set of resource limits of a compose service; a limit left out is "
        "removed. cpus is in cores (0.5, 2) and at most the host's CPUs; memory and "
        "memory_reservation are sizes such as 512m or 2g (at least 6m, at most the host's "
        "memory); cpu_shares is a relative weight (default 1024). The limits survive every "
        "deploy. With apply_now the running containers are updated immediately, without a "
        "restart; the result lists the outcome per container.",
        TokenScope.CONTAINERS,
        schema.obj(
            {
                **_ENV,
                "service": _SERVICE,
                "cpus": {"type": "number", "exclusiveMinimum": 0, "maximum": 4096},
                "memory": {"type": "string", "minLength": 1, "maxLength": 32},
                "memory_reservation": {"type": "string", "minLength": 1, "maxLength": 32},
                "cpu_shares": {"type": "integer", "minimum": 2, "maximum": 262144},
                "apply_now": _APPLY_NOW,
            },
            [*_REQUIRED_ENV, "service"],
        ),
        set_resource_limits,
        read_only=False,
    ),
    Tool(
        "remove_resource_limits",
        "Remove the saved resource limits of a compose service. Future deploys run it without "
        "them. With apply_now the running containers are raised to the host's capacity (Docker "
        "cannot take a limit off a running container; the next deploy removes it fully).",
        TokenScope.CONTAINERS,
        schema.obj(
            {**_ENV, "service": _SERVICE, "apply_now": _APPLY_NOW},
            [*_REQUIRED_ENV, "service"],
        ),
        remove_resource_limits,
        read_only=False,
    ),
)
