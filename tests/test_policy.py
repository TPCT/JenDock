"""The policy matrix: role x action x resource scope, token scopes and session-only actions.

The expectations are written out here by hand (not read from ``RULES``) so a change of the
rule table has to be a conscious change of this file too.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

import pytest

from backend.domain.access import (
    GLOBAL,
    NO_SERVICE,
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
from backend.services import policy

DEV, PROD = EnvironmentName.DEVELOPMENT, EnvironmentName.PRODUCTION
V, D, M = AccessRole.VIEWER, AccessRole.DEVELOPER, AccessRole.MAINTAINER
ALL_SCOPES = frozenset(TokenScope)


@dataclass(frozen=True)
class Who:
    """A test principal."""

    role: GlobalRole
    grants: tuple[Grant, ...] = ()
    scopes: frozenset[TokenScope] | None = None  # None: a browser session

    @property
    def is_session(self) -> bool:
        return self.scopes is None

    def has_scope(self, scope: TokenScope) -> bool:
        if self.scopes is None:
            return True
        return scope in self.scopes or TokenScope.ADMIN in self.scopes


def member(*grants: Grant, scopes: frozenset[TokenScope] | None = None) -> Who:
    return Who(GlobalRole.MEMBER, grants, scopes)


def grant(env: EnvironmentName | None, role: AccessRole, project: str = "demo") -> Grant:
    return Grant(project, env, None, role)


def svc(env: EnvironmentName, service: str, *perms: ContainerPermission) -> Grant:
    return Grant("demo", env, service, None, frozenset(perms))


# action -> (minimum role on development, minimum role on production)
ROLE_ACTIONS: dict[Action, tuple[AccessRole, AccessRole]] = {
    Action.RUN_READ: (V, V),
    Action.PIPELINE_READ: (V, V),
    Action.DEPLOYMENT_READ: (V, V),
    Action.CONTAINER_READ: (V, V),
    Action.DATABASE_READ: (V, V),
    Action.DOMAIN_READ: (V, V),
    Action.SECRET_LIST: (V, V),
    Action.RUN_CREATE: (D, M),
    Action.RUN_CANCEL: (D, M),
    Action.RUN_RETRY: (D, M),
    Action.PIPELINE_WRITE: (D, M),
    Action.DATABASE_DATA: (D, M),
    Action.DEPLOYMENT_REDEPLOY: (M, M),
    Action.CONTAINER_LOGS: (M, M),
    Action.CONTAINER_TERMINAL: (M, M),
    Action.CONTAINER_ACTION: (M, M),
    Action.CONTAINER_ENV_UPDATE: (M, M),
    Action.CONTAINER_LIMITS: (M, M),
    Action.CONTAINER_FILES: (M, M),
    Action.DOMAIN_MANAGE: (M, M),
    Action.DOMAIN_LOOKUP: (M, M),
    Action.DOMAIN_ADOPT: (M, M),
}
GLOBAL_ADMIN_ACTIONS = {
    Action.PROJECT_CREATE,
    Action.PROJECT_UPDATE,
    Action.ENVIRONMENT_UPDATE,
    Action.CONTAINER_ENV_REVEAL,
    Action.DATABASE_CONFIGURE,
    Action.CERTIFICATE_LIST,
    Action.DOMAIN_DISCOVER,
    Action.DOMAIN_DISCOVER_REFRESH,
    Action.DOMAIN_ADOPT_ALL,
    Action.SECRET_WRITE,
    Action.SECRET_REVEAL,
    Action.USER_MANAGE,
    Action.ACCESS_MANAGE,
    Action.INTEGRATION_MANAGE,
    Action.NOTIFICATION_MANAGE,
}
ANY_USER_ACTIONS = {
    Action.PROJECT_LIST,
    Action.RUN_LIST,
    Action.PIPELINE_TOOLS,
    Action.TOKEN_MANAGE,
    Action.MCP_TOOLS,
    Action.NOTIFICATION_POLICY,
    Action.INGRESS_READINESS,
    Action.ACCOUNT_MANAGE,
    Action.AUDIT_READ,
    Action.DASHBOARD_READ,
}
SERVICE_PERMISSION: dict[Action, ContainerPermission] = {
    Action.CONTAINER_LOGS: ContainerPermission.LOGS,
    Action.CONTAINER_TERMINAL: ContainerPermission.TERMINAL,
    Action.CONTAINER_ACTION: ContainerPermission.ACTIONS,
}
SESSION_ONLY = {
    Action.TOKEN_MANAGE,
    Action.USER_MANAGE,
    Action.ACCESS_MANAGE,
    Action.INTEGRATION_MANAGE,
    Action.MCP_TOOLS,
    Action.ACCOUNT_MANAGE,
    Action.AUDIT_READ,
}


def allowed(who: Who, action: Action, resource: Resource = GLOBAL) -> bool:
    return policy.is_allowed(who, action, resource)


def test_every_action_is_classified_exactly_once() -> None:
    classified = (
        set(ROLE_ACTIONS)
        | GLOBAL_ADMIN_ACTIONS
        | ANY_USER_ACTIONS
        | {Action.PROJECT_READ, Action.TELEMETRY_READ}
    )
    assert classified == set(Action)
    assert (
        len(classified) == len(ROLE_ACTIONS) + len(GLOBAL_ADMIN_ACTIONS) + len(ANY_USER_ACTIONS) + 2
    )
    assert {a for a, r in RULES.items() if r.kind is RuleKind.ROLE} == set(ROLE_ACTIONS)


@pytest.mark.parametrize("role", [GlobalRole.OWNER, GlobalRole.ADMIN])
@pytest.mark.parametrize("action", list(Action))
@pytest.mark.parametrize("env", [DEV, PROD])
def test_owner_and_admin_can_do_everything_on_everything(
    role: GlobalRole, action: Action, env: EnvironmentName
) -> None:
    who = Who(role)
    assert allowed(who, action, Resource.of_environment("anything", env))
    assert allowed(who, action, Resource.of_service("anything", env, "web"))
    assert allowed(who, action, GLOBAL)


@pytest.mark.parametrize("action", sorted(ROLE_ACTIONS, key=str))
@pytest.mark.parametrize("held", [V, D, M])
@pytest.mark.parametrize("scope", ["project", "same_env", "other_env"])
@pytest.mark.parametrize("target", [DEV, PROD])
def test_member_role_matrix(
    action: Action, held: AccessRole, scope: str, target: EnvironmentName
) -> None:
    other = PROD if target is DEV else DEV
    granted = {"project": None, "same_env": target, "other_env": other}[scope]
    who = member(grant(granted, held))
    minimum = ROLE_ACTIONS[action][0 if target is DEV else 1]
    covers = scope != "other_env"
    expected = covers and held.rank >= minimum.rank
    assert allowed(who, action, Resource.of_environment("demo", target)) is expected


@pytest.mark.parametrize("action", sorted(ROLE_ACTIONS, key=str))
def test_roles_are_cumulative_and_environment_grants_add_up(action: Action) -> None:
    who = member(grant(None, V), grant(PROD, M))
    prod_ok = allowed(who, action, Resource.of_environment("demo", PROD))
    dev_ok = allowed(who, action, Resource.of_environment("demo", DEV))
    assert prod_ok is True  # maintainer on production covers every role action there
    assert dev_ok is (ROLE_ACTIONS[action][0] is V)  # on development only the project viewer


@pytest.mark.parametrize("action", sorted(GLOBAL_ADMIN_ACTIONS, key=str))
def test_members_never_pass_admin_only_actions(action: Action) -> None:
    who = member(grant(None, M))
    assert not allowed(who, action, Resource.of_environment("demo", PROD))
    assert not allowed(who, action, GLOBAL)
    with pytest.raises(PermissionDeniedError):
        policy.authorize(who, action, GLOBAL)


@pytest.mark.parametrize("action", sorted(ANY_USER_ACTIONS, key=str))
def test_any_user_actions_need_no_grant(action: Action) -> None:
    assert allowed(member(), action, GLOBAL)


def test_telemetry_needs_a_grant_for_members() -> None:
    assert not allowed(member(), Action.TELEMETRY_READ)
    assert allowed(member(grant(DEV, V)), Action.TELEMETRY_READ)


def test_production_data_needs_a_maintainer_grant_on_production() -> None:
    prod = Resource.of_environment("demo", PROD)
    assert not allowed(member(grant(None, D)), Action.DATABASE_DATA, prod)
    assert not allowed(member(grant(DEV, M)), Action.DATABASE_DATA, prod)
    assert allowed(member(grant(PROD, M)), Action.DATABASE_DATA, prod)
    assert allowed(
        member(grant(DEV, D)), Action.DATABASE_DATA, Resource.of_environment("demo", DEV)
    )
    assert allowed(Who(GlobalRole.ADMIN), Action.DATABASE_DATA, prod)


def test_production_pipeline_and_runs_need_maintainer() -> None:
    dev, prod = (Resource.of_environment("demo", e) for e in (DEV, PROD))
    dev_user = member(grant(None, D))
    assert allowed(dev_user, Action.PIPELINE_WRITE, dev)
    assert not allowed(dev_user, Action.PIPELINE_WRITE, prod)
    assert allowed(dev_user, Action.RUN_CREATE, dev)
    assert not allowed(dev_user, Action.RUN_CREATE, prod)


@pytest.mark.parametrize("action", sorted(SERVICE_PERMISSION, key=str))
@pytest.mark.parametrize("perm", list(ContainerPermission))
def test_container_grants_allow_exactly_their_permission(
    action: Action, perm: ContainerPermission
) -> None:
    who = member(svc(DEV, "web", perm))
    on_web = Resource.of_service("demo", DEV, "web")
    assert allowed(who, action, on_web) is (SERVICE_PERMISSION[action] is perm)
    # never another service, another environment, or role-only actions
    assert not allowed(who, action, Resource.of_service("demo", DEV, "worker"))
    assert not allowed(who, action, Resource.of_service("demo", PROD, "web"))
    assert not allowed(who, action, Resource(project="demo", environment=DEV, service=NO_SERVICE))


@pytest.mark.parametrize(
    "action",
    [a for a in ROLE_ACTIONS if a not in SERVICE_PERMISSION and a is not Action.CONTAINER_READ],
)
def test_container_grants_allow_nothing_else(action: Action) -> None:
    who = member(svc(DEV, "web", *ContainerPermission))
    assert not allowed(who, action, Resource.of_service("demo", DEV, "web"))
    assert not allowed(who, action, Resource.of_environment("demo", DEV))


def test_a_container_grant_makes_the_container_and_project_visible_only() -> None:
    who = member(svc(DEV, "web", ContainerPermission.LOGS))
    assert allowed(who, Action.PROJECT_READ, Resource.of_project("demo"))
    assert allowed(who, Action.PROJECT_READ, Resource.of_environment("demo", DEV))
    assert not allowed(who, Action.PROJECT_READ, Resource.of_environment("demo", PROD))
    assert allowed(who, Action.CONTAINER_READ, Resource.of_service("demo", DEV, "web"))
    assert not allowed(who, Action.CONTAINER_READ, Resource.of_service("demo", DEV, "worker"))
    assert not allowed(who, Action.RUN_READ, Resource.of_environment("demo", DEV))


def test_invisible_projects_are_not_found_not_forbidden() -> None:
    who = member(grant(None, M, project="other"))
    with pytest.raises(NotFoundError):
        policy.authorize(who, Action.RUN_READ, Resource.of_environment("demo", DEV))
    with pytest.raises(NotFoundError):
        policy.authorize(member(), Action.PROJECT_READ, Resource.of_project("demo"))
    visible = member(grant(DEV, V))
    with pytest.raises(NotFoundError):  # production is hidden from a development-only grant
        policy.authorize(visible, Action.RUN_READ, Resource.of_environment("demo", PROD))
    with pytest.raises(PermissionDeniedError):
        policy.authorize(visible, Action.RUN_CREATE, Resource.of_environment("demo", DEV))


@pytest.mark.parametrize("action", sorted(ROLE_ACTIONS, key=str))
def test_token_scopes_narrow_what_the_user_may_do(action: Action) -> None:
    scope = RULES[action].scope
    assert scope is not None
    resource = Resource.of_environment("demo", DEV)
    wrong = TokenScope.RUN if scope is TokenScope.READ else TokenScope.READ
    assert not allowed(Who(GlobalRole.OWNER, scopes=frozenset({wrong})), action, resource)
    assert allowed(Who(GlobalRole.OWNER, scopes=frozenset({scope})), action, resource)
    assert allowed(Who(GlobalRole.OWNER, scopes=frozenset({TokenScope.ADMIN})), action, resource)


@pytest.mark.parametrize("action", sorted(ROLE_ACTIONS, key=str))
def test_a_token_never_exceeds_its_owner(action: Action) -> None:
    resource = Resource.of_environment("demo", DEV)
    nothing = member(scopes=ALL_SCOPES)  # every scope, but a member without grants
    assert not allowed(nothing, action, resource)
    viewer = member(grant(None, V), scopes=ALL_SCOPES)
    session_viewer = member(grant(None, V))
    assert allowed(viewer, action, resource) == allowed(session_viewer, action, resource)
    assert allowed(viewer, action, resource) is (ROLE_ACTIONS[action][0] is V)


def test_production_data_needs_the_admin_scope_on_a_token() -> None:
    prod = Resource.of_environment("demo", PROD)
    owner = Who(GlobalRole.OWNER, scopes=frozenset({TokenScope.READ}))
    assert not allowed(owner, Action.DATABASE_DATA, prod)
    assert allowed(owner, Action.DATABASE_DATA, Resource.of_environment("demo", DEV))
    assert allowed(
        Who(GlobalRole.OWNER, scopes=frozenset({TokenScope.ADMIN})), Action.DATABASE_DATA, prod
    )


@pytest.mark.parametrize("action", sorted(SESSION_ONLY, key=str))
def test_session_only_actions_reject_tokens_even_with_admin_scope(action: Action) -> None:
    token = Who(GlobalRole.OWNER, scopes=frozenset({TokenScope.ADMIN}))
    with pytest.raises(PermissionDeniedError, match="browser session"):
        policy.authorize(token, action, GLOBAL)
    assert allowed(Who(GlobalRole.OWNER), action, GLOBAL)


def test_missing_scope_is_reported_before_visibility() -> None:
    token = member(scopes=frozenset({TokenScope.READ}))
    with pytest.raises(PermissionDeniedError, match="missing scope: run"):
        policy.authorize(token, Action.RUN_CREATE, Resource.of_environment("hidden", DEV))


def test_user_management_rules() -> None:
    owner, admin, mem = GlobalRole.OWNER, GlobalRole.ADMIN, GlobalRole.MEMBER
    for target in (None, owner, admin, mem):
        policy.require_user_management(owner, target, admin)
    policy.require_user_management(admin, mem, admin)
    policy.require_user_management(admin, admin, mem)
    for args in ((admin, owner, None), (admin, None, owner), (admin, mem, owner)):
        with pytest.raises(PermissionDeniedError):
            policy.require_user_management(*args)
    with pytest.raises(PermissionDeniedError):
        policy.require_user_management(mem, mem, mem)


def test_summary_for_members_lists_what_the_ui_may_show() -> None:
    who = member(grant(DEV, D), svc(PROD, "web", ContainerPermission.LOGS))
    summary = policy.summarize(who)
    assert summary["role"] == "member" and summary["all"] is False
    projects = cast(dict[str, Any], summary["projects"])
    environments = projects["demo"]["environments"]
    assert environments["development"]["role"] == "developer"
    assert "run.create" in environments["development"]["actions"]
    assert "deployment.redeploy" not in environments["development"]["actions"]
    assert environments["production"]["role"] is None
    web = environments["production"]["services"]["web"]
    assert web == ["container.logs", "container.read", "project.read"]
    assert "user.manage" not in cast(list[str], summary["global"])
    owner = policy.summarize(Who(GlobalRole.OWNER))
    assert owner["all"] is True and "user.manage" in cast(list[str], owner["global"])
