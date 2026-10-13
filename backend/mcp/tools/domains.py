"""Domains and ingress: status, checks, previewed apply/remove and certificates.

Scopes mirror the REST routes: reads need ``read``; checks, previews and every change need
``admin``. Changes are two steps: ``preview_domain_change`` returns the DNS and vhost plan with its
``plan_hash``, and ``apply_domain`` / ``remove_domain`` only act on that exact hash. DNS, Apache and
TLS work happens in the worker; no certificate key or API token ever passes through here.
"""

from __future__ import annotations

from backend.api.schemas.discovery import (
    AdoptSuggestedResponse,
    DiscoveredPage,
    DiscoveryStateResponse,
)
from backend.api.schemas.domains import (
    CertificateOverviewResponse,
    CertificateResponse,
    CheckResponse,
    DomainDetailResponse,
    DomainResponse,
    PlanResponse,
)
from backend.domain.discovery import LinkFilter
from backend.domain.enums import TokenScope
from backend.domain.errors import ValidationError
from backend.domain.ingress import DomainOrigin
from backend.mcp import schema
from backend.mcp.registry import Args, Tool, ToolContext
from backend.services.domain_adoption import AdoptRequest

_TARGET = {
    "slug": schema.SLUG,
    "environment": schema.ENVIRONMENT,
    "domain_id": schema.RUN_ID,
}
_REQUIRED = ["slug", "environment", "domain_id"]
_PLAN_HASH = {"type": "string", "pattern": "^[0-9a-f]{64}$"}


def _known(ctx: ToolContext, args: Args) -> int:
    """The domain id, after checking it belongs to the environment (404 otherwise)."""
    domain_id = args.integer("domain_id")
    ctx.container.domain_service().get(args.text("slug"), args.environment(), domain_id)
    return domain_id


def list_domains(ctx: ToolContext, args: Args) -> object:
    """The domains of an environment, or (without slug and environment) every discovered domain."""
    slug, env = args.optional_text("slug"), args.optional_environment()
    if (slug is None) != (env is None):
        raise ValidationError("give both slug and environment, or neither")
    if slug is None or env is None:
        link = args.optional_text("link")
        page = ctx.container.discovery_queries().page(
            args.page_query(), LinkFilter(link) if link else None, args.optional_text("zone")
        )
        return DiscoveredPage.from_page(page).model_dump(mode="json")
    views = ctx.container.domain_status_service().list(slug, env)
    return [DomainDetailResponse.from_status(view).model_dump(mode="json") for view in views]


def discover_domains(ctx: ToolContext, _args: Args) -> object:
    """Read Cloudflare (zones and DNS records) and Apache again; returns what could be read."""
    ctx.container.discovery.refresh(ctx.actor)
    state = ctx.container.discovery_queries().state()
    return DiscoveryStateResponse.from_view(state).model_dump(mode="json")


def adopt_domain(ctx: ToolContext, args: Args) -> object:
    """Record a discovered domain as linked to a project environment (no DNS/Apache change)."""
    request = AdoptRequest(
        args.text("hostname"),
        args.text("service"),
        args.integer("container_port"),
        args.integer("published_port"),
    )
    domain = ctx.container.adoption_service().adopt(
        args.text("slug"), args.environment(), request, ctx.actor
    )
    return DomainResponse.from_domain(domain).model_dump(mode="json")


def adopt_suggested_domains(ctx: ToolContext, args: Args) -> object:
    """Adopt every discovered domain whose project is certain (all, or only ``hostnames``)."""
    names = args.values.get("hostnames")
    wanted = [str(n) for n in names] if isinstance(names, list) else None
    result = ctx.container.adoption_service().adopt_suggested(wanted, ctx.actor)
    return AdoptSuggestedResponse.from_result(result).model_dump(mode="json")


def get_domain(ctx: ToolContext, args: Args) -> object:
    """One domain with certificate health and the latest check."""
    view = ctx.container.domain_status_service().status(
        args.text("slug"), args.environment(), args.integer("domain_id")
    )
    return DomainDetailResponse.from_status(view).model_dump(mode="json")


def list_certificates(ctx: ToolContext, _args: Args) -> object:
    """Certificate expiry across all domains, soonest first."""
    items = ctx.container.domain_status_service().certificates()
    return [CertificateOverviewResponse.from_overview(i).model_dump(mode="json") for i in items]


def check_domain(ctx: ToolContext, args: Args) -> object:
    """Run the DNS, HTTPS and backend checks now."""
    domain = ctx.container.domain_service().get(
        args.text("slug"), args.environment(), args.integer("domain_id")
    )
    if domain.origin is DomainOrigin.ADOPTED:  # read-only checks: DNS record, Apache, HTTPS
        result = ctx.container.discovery.verify(domain.id, ctx.actor)
    else:
        result = ctx.container.ingress.check(domain.id, ctx.actor)
    return CheckResponse.from_result(result).model_dump(mode="json")


def preview_domain_change(ctx: ToolContext, args: Args) -> object:
    """The DNS, Apache vhost and TLS plan of an apply or a remove, with its plan_hash."""
    domain_id = _known(ctx, args)
    operation = args.optional_text("operation") or "apply"
    plan = ctx.container.ingress.preview(domain_id, operation, ctx.actor)
    return PlanResponse.from_view(plan).model_dump(mode="json")


def apply_domain(ctx: ToolContext, args: Args) -> object:
    """Apply the previewed plan (refused when the plan changed since the preview)."""
    domain_id = _known(ctx, args)
    plan = ctx.container.ingress.apply(domain_id, args.text("plan_hash"), ctx.actor)
    return PlanResponse.from_view(plan).model_dump(mode="json")


def remove_domain(ctx: ToolContext, args: Args) -> object:
    """Remove the previewed domain from Apache, TLS and DNS (own records only)."""
    domain_id = _known(ctx, args)
    plan = ctx.container.ingress.remove(domain_id, args.text("plan_hash"), ctx.actor)
    return PlanResponse.from_view(plan).model_dump(mode="json")


def issue_certificate(ctx: ToolContext, args: Args) -> object:
    """Issue or renew the certificate now."""
    domain_id = _known(ctx, args)
    issued = ctx.container.ingress.issue_certificate(domain_id, ctx.actor)
    return CertificateResponse(
        not_after=issued.not_after,
        days_remaining=issued.days_remaining,
        state=issued.state,
        last_attempt_at=None,
        last_error=None,
    ).model_dump(mode="json")


TOOLS = (
    Tool(
        "list_domains",
        "List the domains of an environment with their status: hostname, service, certificate "
        "state and expiry, and the latest DNS/HTTPS/backend check. Without slug and environment "
        "(owners and admins) it pages through every domain discovered on Cloudflare and Apache "
        "(filter link=linked|unlinked|suggested, zone, q), each with its suggested project.",
        TokenScope.READ,
        schema.obj(
            {
                "slug": schema.SLUG,
                "environment": schema.ENVIRONMENT,
                "link": {"type": "string", "enum": [item.value for item in LinkFilter]},
                "zone": {"type": "string", "maxLength": 253},
                **schema.paging(("hostname", "zone")),
            }
        ),
        list_domains,
    ),
    Tool(
        "discover_domains",
        "Read Cloudflare (zones and A/AAAA/CNAME records) and, when the host helper is "
        "installed, the Apache sites again, and match them to projects. Read-only toward "
        "Cloudflare and Apache: it only refreshes Jendock's list of discovered domains.",
        TokenScope.ADMIN,
        schema.obj(),
        discover_domains,
        read_only=False,
    ),
    Tool(
        "adopt_domain",
        "Record a discovered domain as linked to a project environment, WITHOUT changing DNS "
        "or Apache (it was set up outside Jendock and stays that way). Needs the service and "
        "the container and published ports; list_domains (no slug) shows suggestions.",
        TokenScope.ADMIN,
        schema.obj(
            {
                "slug": schema.SLUG,
                "environment": schema.ENVIRONMENT,
                "hostname": {"type": "string", "minLength": 1, "maxLength": 253},
                "service": {"type": "string", "minLength": 1, "maxLength": 63},
                "container_port": {"type": "integer", "minimum": 1, "maximum": 65535},
                "published_port": {"type": "integer", "minimum": 1, "maximum": 65535},
            },
            ["slug", "environment", "hostname", "service", "container_port", "published_port"],
        ),
        adopt_domain,
        read_only=False,
    ),
    Tool(
        "adopt_suggested_domains",
        "Adopt every unlinked discovered domain whose project is certain (Apache proxies to "
        "the published port of exactly one project container), or only the given hostnames. "
        "Owners and admins only.",
        TokenScope.ADMIN,
        schema.obj({"hostnames": {"type": "array", "items": {"type": "string"}, "maxItems": 500}}),
        adopt_suggested_domains,
        read_only=False,
    ),
    Tool(
        "get_domain",
        "Get one domain with its certificate health and latest check.",
        TokenScope.READ,
        schema.obj(_TARGET, _REQUIRED),
        get_domain,
    ),
    Tool(
        "list_certificates",
        "Certificate expiry across all domains of all projects, soonest expiry first.",
        TokenScope.READ,
        schema.obj(),
        list_certificates,
    ),
    Tool(
        "check_domain",
        "Run the DNS, HTTPS and backend checks of a domain now and store the result.",
        TokenScope.ADMIN,
        schema.obj(_TARGET, _REQUIRED),
        check_domain,
        read_only=False,
    ),
    Tool(
        "preview_domain_change",
        "Step 1 of 2: show the DNS records, Apache vhost diff and TLS steps that adding "
        "(operation apply) or removing (operation remove) a domain would make, with the "
        "plan_hash to pass to apply_domain or remove_domain. Nothing is changed.",
        TokenScope.ADMIN,
        schema.obj(
            {**_TARGET, "operation": {"type": "string", "enum": ["apply", "remove"]}}, _REQUIRED
        ),
        preview_domain_change,
        read_only=False,
    ),
    Tool(
        "apply_domain",
        "Step 2 of 2: apply the previewed plan to DNS, Apache and TLS. plan_hash must be the one "
        "preview_domain_change returned (operation apply); a changed plan is refused.",
        TokenScope.ADMIN,
        schema.obj({**_TARGET, "plan_hash": _PLAN_HASH}, [*_REQUIRED, "plan_hash"]),
        apply_domain,
        read_only=False,
    ),
    Tool(
        "remove_domain",
        "Step 2 of 2 for removal: remove the previewed domain from Apache, TLS and DNS (only "
        "records this platform created). plan_hash must come from preview_domain_change with "
        "operation remove.",
        TokenScope.ADMIN,
        schema.obj({**_TARGET, "plan_hash": _PLAN_HASH}, [*_REQUIRED, "plan_hash"]),
        remove_domain,
        read_only=False,
    ),
    Tool(
        "issue_certificate",
        "Issue or renew the TLS certificate of an applied domain now.",
        TokenScope.ADMIN,
        schema.obj(_TARGET, _REQUIRED),
        issue_certificate,
        read_only=False,
    ),
)
