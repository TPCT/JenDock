"""Glue between the pure policy (``backend.services.policy``) and what a request refers to.

Routers and MCP tools name an action and the resource a request points at; this module resolves
runs and containers to their project, environment and service and then asks the policy. It adds
no rules of its own.
"""

from __future__ import annotations

from collections.abc import Sequence

from backend.api.container import AppContainer
from backend.api.principal import Principal
from backend.containers.wire import ContainerView
from backend.domain.access import GLOBAL, NO_SERVICE, Action, Resource
from backend.domain.enums import EnvironmentName
from backend.domain.errors import DomainError, PermissionDeniedError
from backend.services import policy
from backend.services.project_queries import EnvironmentPredicate, ProjectFilter


def authorize(principal: Principal, action: Action, resource: Resource = GLOBAL) -> None:
    """Raise unless the principal may perform ``action`` on ``resource``."""
    policy.authorize(principal, action, resource)


def authorize_run(
    container: AppContainer, principal: Principal, action: Action, run_id: int
) -> Resource:
    """Authorize ``action`` on the environment a run belongs to; returns that resource."""
    slug, environment = container.run_queries().locate(run_id)
    resource = Resource.of_environment(slug, environment)
    policy.authorize(principal, action, resource)
    return resource


def authorize_container(  # noqa: PLR0913, PLR0917 - action plus the container's full address
    container: AppContainer,
    principal: Principal,
    action: Action,
    slug: str,
    environment: EnvironmentName,
    container_id: str,
) -> None:
    """Authorize ``action`` on one container.

    A role on the project or environment decides first. Only when it does not allow the action
    and the member holds container grants in that environment, the container's compose service
    is looked up (through the worker) and the grants of that service are consulted.
    """
    by_role = Resource(slug, environment, NO_SERVICE)
    try:
        policy.authorize(principal, action, by_role)
    except PermissionDeniedError:
        if principal.role.is_privileged or not policy.has_service_grants(
            principal, slug, environment
        ):
            raise
        info = container.container_gateway().get_container(
            slug, environment, container_id, principal.actor
        )
        if info.service is None:
            raise
        policy.authorize(principal, action, Resource.of_service(slug, environment, info.service))


def project_filter(principal: Principal) -> ProjectFilter | None:
    """What the principal may see in project lists (``None``: everything)."""
    if principal.role.is_privileged:
        return None

    def sees(slug: str, environment: EnvironmentName) -> bool:
        return policy.sees(principal, slug, environment)

    def reads_runs(slug: str, environment: EnvironmentName) -> bool:
        return policy.is_allowed(principal, Action.RUN_READ, Resource(slug, environment))

    return ProjectFilter(sees, reads_runs)


def run_predicate(principal: Principal) -> EnvironmentPredicate | None:
    """Which environments' runs the principal may read (``None``: all of them)."""
    if principal.role.is_privileged:
        return None

    def reads_runs(slug: str, environment: EnvironmentName) -> bool:
        return policy.is_allowed(principal, Action.RUN_READ, Resource(slug, environment))

    return reads_runs


def refresh(container: AppContainer, principal: Principal) -> Principal | None:
    """The principal as it is now (role, grants, enabled), or ``None`` once it is invalid.

    Long-lived streams call this at every heartbeat so a revoked grant, a disabled user or an
    ended session stops them. Resolving a session here never extends its idle timer.
    """
    if principal.session is not None and principal.session_token is not None:
        context = container.session_service().resolve(principal.session_token, touch=False)
        if context is None or context.must_change_password:
            return None
        grants = (
            () if context.role.is_privileged else container.access_service().load(context.user_id)
        )
        return Principal(session=context, session_token=principal.session_token, grants=grants)
    if principal.token is not None:
        token = container.token_service().current(principal.token.id)
        if token is None:
            return None
        grants = (
            ()
            if token.owner_role.is_privileged
            else container.access_service().load(token.owner_id)
        )
        return Principal(token=token, grants=grants)
    return None


def still_allowed(
    container: AppContainer, principal: Principal, action: Action, resource: Resource
) -> bool:
    """Whether the principal, as it is now, may still perform ``action`` on ``resource``."""
    current = refresh(container, principal)
    return current is not None and policy.is_allowed(current, action, resource)


def container_still_allowed(  # noqa: PLR0913, PLR0917 - action plus the container's address
    container: AppContainer,
    principal: Principal,
    action: Action,
    slug: str,
    environment: EnvironmentName,
    container_id: str,
) -> bool:
    """Like ``still_allowed`` for one container (resolving its service when needed)."""
    current = refresh(container, principal)
    if current is None:
        return False
    try:
        authorize_container(container, current, action, slug, environment, container_id)
    except DomainError:
        return False
    return True


def visible_containers(
    principal: Principal,
    slug: str,
    environment: EnvironmentName,
    views: Sequence[ContainerView],
) -> list[ContainerView]:
    """The containers of an environment the principal may know of (a member with only container
    grants sees just the granted services)."""
    if principal.role.is_privileged:
        return list(views)
    return [
        view
        for view in views
        if policy.is_allowed(
            principal,
            Action.CONTAINER_READ,
            Resource(slug, environment, view.service or NO_SERVICE),
        )
    ]
