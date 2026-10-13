"""Domains and ingress: status, checks, previewed apply/remove and certificates.

Scopes mirror the REST routes: reads need ``read``; checks, previews and every change need
``admin``. Changes are two steps: ``preview_domain_change`` returns the DNS and vhost plan with its
``plan_hash``, and ``apply_domain`` / ``remove_domain`` only act on that exact hash. DNS, Apache and
TLS work happens in the worker; no certificate key or API token ever passes through here.
"""

from __future__ import annotations

from shiftcodes.api.schemas.domains import (
    CertificateOverviewResponse,
    CertificateResponse,
    CheckResponse,
    DomainDetailResponse,
    PlanResponse,
)
from shiftcodes.domain.enums import TokenScope
from shiftcodes.mcp import schema
from shiftcodes.mcp.registry import Args, Tool, ToolContext

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
    """The domains of an environment with certificate health and the latest check."""
    views = ctx.container.domain_status_service().list(args.text("slug"), args.environment())
    return [DomainDetailResponse.from_status(view).model_dump(mode="json") for view in views]


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
    domain_id = _known(ctx, args)
    result = ctx.container.ingress.check(domain_id, ctx.actor)
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
        "state and expiry, and the latest DNS/HTTPS/backend check.",
        TokenScope.READ,
        schema.obj({"slug": schema.SLUG, "environment": schema.ENVIRONMENT}, _REQUIRED[:2]),
        list_domains,
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
