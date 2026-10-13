"""The one authorization rule set: ``authorize(principal, action, resource)``.

Every REST router and every MCP tool decides through this module; there are no ad-hoc role or
scope checks elsewhere. The effective permission of a request is the **intersection** of

1. the API-token scopes (a browser session holds every scope), and
2. what the acting user may do: the global role (owners and admins can do everything, members
   only what their grants give).

A token therefore never exceeds its owner. A member that cannot see a project at all gets
``NotFoundError`` (the project's existence is not revealed); one that can see it but lacks the
permission gets ``PermissionDeniedError``.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from backend.domain.access import (
    GLOBAL,
    RULES,
    AccessRole,
    Action,
    ContainerPermission,
    GlobalRole,
    Grant,
    Resource,
    RuleKind,
)
from backend.domain.enums import EnvironmentName, TokenScope
from backend.domain.errors import NotFoundError, PermissionDeniedError

ENVIRONMENTS = (EnvironmentName.DEVELOPMENT, EnvironmentName.PRODUCTION)


class Subject(Protocol):
    """What the policy needs to know about the caller (``Principal`` implements it)."""

    @property
    def is_session(self) -> bool:
        """Whether the caller is a browser session."""
        ...

    @property
    def role(self) -> GlobalRole:
        """The acting user's global role."""
        ...

    @property
    def grants(self) -> tuple[Grant, ...]:
        """The acting user's access grants."""
        ...

    def has_scope(self, scope: TokenScope) -> bool:
        """Whether the credential holds ``scope`` (sessions hold every scope)."""
        ...


def _grants_for(subject: Subject, project: str) -> list[Grant]:
    return [grant for grant in subject.grants if grant.project == project]


def role_on(
    subject: Subject, project: str, environment: EnvironmentName | None
) -> AccessRole | None:
    """The best project/environment role of a member (service grants do not count)."""
    best: AccessRole | None = None
    for grant in _grants_for(subject, project):
        if grant.role is None or grant.service is not None:
            continue
        if grant.environment is not None and grant.environment is not environment:
            continue
        if best is None or grant.role.rank > best.rank:
            best = grant.role
    return best


def _service_permissions(
    subject: Subject, project: str, environment: EnvironmentName, service: str | None
) -> set[ContainerPermission]:
    found: set[ContainerPermission] = set()
    for grant in _grants_for(subject, project):
        if grant.service is None or grant.environment is not environment:
            continue
        if service is None or grant.service == service:
            found |= grant.permissions
    return found


def sees(subject: Subject, project: str, environment: EnvironmentName | None = None) -> bool:
    """Whether a principal may know a project (or one of its environments) exists."""
    if subject.role.is_privileged:
        return True
    for grant in _grants_for(subject, project):
        if environment is None or grant.environment in (None, environment):
            return True
    return False


def _check_scope(subject: Subject, action: Action, resource: Resource) -> None:
    rule = RULES[action]
    scope = rule.scope_for(resource.environment)
    if scope is None:
        if not subject.is_session:
            raise PermissionDeniedError("this operation requires a browser session")
        return
    if not subject.has_scope(scope):
        raise PermissionDeniedError(f"missing scope: {scope.value}")


def _member_allows(subject: Subject, action: Action, resource: Resource) -> bool:
    rule = RULES[action]
    if rule.kind is RuleKind.GLOBAL_ADMIN:
        return False
    if rule.kind is RuleKind.ANY_USER:
        return True
    if rule.kind is RuleKind.ANY_GRANT:
        return bool(subject.grants)
    project = resource.project
    if project is None:
        return False
    if rule.kind is RuleKind.VISIBLE:
        return sees(subject, project, resource.environment)
    return _role_allows(subject, action, resource, project)


def _role_allows(subject: Subject, action: Action, resource: Resource, project: str) -> bool:
    rule = RULES[action]
    environment = resource.environment
    held = role_on(subject, project, environment)
    if held is not None and held.rank >= rule.minimum(environment).rank:
        return True
    if environment is None or (rule.service is None and not rule.any_service):
        return False
    granted = _service_permissions(subject, project, environment, resource.service)
    if rule.any_service:
        return bool(granted)
    return rule.service in granted


def is_allowed(subject: Subject, action: Action, resource: Resource = GLOBAL) -> bool:
    """Whether the action is allowed (never raises)."""
    try:
        authorize(subject, action, resource)
    except (PermissionDeniedError, NotFoundError):
        return False
    return True


def authorize(subject: Subject, action: Action, resource: Resource = GLOBAL) -> None:
    """Raise unless ``subject`` may perform ``action`` on ``resource``.

    ``PermissionDeniedError`` for a missing token scope, a session-only operation or a missing
    permission on a visible resource; ``NotFoundError`` when the member cannot see the project.
    """
    _check_scope(subject, action, resource)
    if subject.role.is_privileged:
        return
    if resource.project is not None and not sees(subject, resource.project, resource.environment):
        what = "environment" if resource.environment else "project"
        name = resource.project
        if resource.environment:
            name = f"{resource.project}/{resource.environment.value}"
        raise NotFoundError(f"{what} not found: {name}")
    if not _member_allows(subject, action, resource):
        raise PermissionDeniedError(f"you do not have permission to {action.value}")


def could_perform(subject: Subject, action: Action) -> bool:
    """Whether the principal might be allowed ``action`` on some resource (for tool listings)."""
    rule = RULES[action]
    if rule.scope is not None and not subject.has_scope(rule.scope):
        return False
    if subject.role.is_privileged:
        return True
    if rule.kind is RuleKind.GLOBAL_ADMIN:
        return False
    return rule.kind is RuleKind.ANY_USER or bool(subject.grants)


def has_service_grants(subject: Subject, project: str, environment: EnvironmentName) -> bool:
    """Whether a member holds any container-level grant in the environment."""
    return bool(_service_permissions(subject, project, environment, None))


def required_scope(action: Action, resource: Resource = GLOBAL) -> TokenScope | None:
    """The token scope an action needs for this resource (``None``: sessions only)."""
    return RULES[action].scope_for(resource.environment)


def can_manage_user(actor_role: GlobalRole, target_role: GlobalRole | None) -> bool:
    """Admins cannot create, change, disable, delete or reset owners; owners can do anything."""
    if actor_role is GlobalRole.OWNER:
        return True
    if actor_role is GlobalRole.ADMIN:
        return target_role is not GlobalRole.OWNER
    return False


def require_user_management(
    actor_role: GlobalRole,
    target_role: GlobalRole | None = None,
    new_role: GlobalRole | None = None,
) -> None:
    """Raise unless the actor may manage a user (of ``target_role``) and give ``new_role``."""
    if not can_manage_user(actor_role, target_role) or not can_manage_user(actor_role, new_role):
        raise PermissionDeniedError("only an owner can manage owners")


def _actions_for(subject: Subject, resource: Resource, candidates: Iterable[Action]) -> list[str]:
    return sorted(a.value for a in candidates if is_allowed(subject, a, resource))


def summarize(subject: Subject) -> dict[str, object]:
    """The effective permission summary the UI uses to hide actions the user cannot perform.

    ``global``: actions that need no resource. ``projects`` (members only): per project the
    allowed project actions, per environment its actions and per service its container actions.
    Owners and admins have ``all: true`` and need no per-project list. Token scopes are applied.
    """
    global_actions = [
        a
        for a, r in RULES.items()
        if r.kind in {RuleKind.GLOBAL_ADMIN, RuleKind.ANY_USER, RuleKind.ANY_GRANT}
    ]
    summary: dict[str, object] = {
        "role": subject.role.value,
        "all": subject.role.is_privileged,
        "global": _actions_for(subject, GLOBAL, global_actions),
    }
    projects: dict[str, object] = {}
    if not subject.role.is_privileged:
        for project in sorted({grant.project for grant in subject.grants}):
            projects[project] = _project_summary(subject, project)
    summary["projects"] = projects
    return summary


def _project_summary(subject: Subject, project: str) -> dict[str, object]:
    environment_actions = [
        a for a, r in RULES.items() if r.kind in {RuleKind.ROLE, RuleKind.VISIBLE}
    ]
    environments: dict[str, object] = {}
    for environment in ENVIRONMENTS:
        if not sees(subject, project, environment):
            continue
        resource = Resource.of_environment(project, environment)
        services: dict[str, list[str]] = {}
        for grant in _grants_for(subject, project):
            if grant.service is not None and grant.environment is environment:
                scoped = Resource.of_service(project, environment, grant.service)
                services[grant.service] = _actions_for(subject, scoped, environment_actions)
        environments[environment.value] = {
            "role": _role_name(role_on(subject, project, environment)),
            "actions": _actions_for(subject, resource, environment_actions),
            "services": services,
        }
    return {"environments": environments}


def _role_name(role: AccessRole | None) -> str | None:
    return role.value if role else None
