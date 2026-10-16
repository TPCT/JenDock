"""Access-control vocabulary: global roles, per-resource roles, actions and the rule table.

Pure data, no I/O. ``backend.services.policy`` evaluates these rules; nothing else decides
who may do what.

Global roles (``users.role``):

- ``owner``: everything, including managing owners.
- ``admin``: everything except creating, changing, disabling or deleting owners.
- ``member``: only what access grants give.

Grants (``access_grants``) give a member a role on a project (all its environments), on one
environment of a project, or fine-grained container permissions on one compose service of an
environment. Roles are cumulative: ``viewer`` < ``developer`` < ``maintainer``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from backend.domain.enums import EnvironmentName, TokenScope


class GlobalRole(StrEnum):
    """A user's platform-wide role."""

    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"

    @property
    def is_privileged(self) -> bool:
        """Owners and admins hold every action on every resource."""
        return self is not GlobalRole.MEMBER


class AccessRole(StrEnum):
    """What a grant on a project or environment allows (cumulative, like GitHub repo roles)."""

    VIEWER = "viewer"
    DEVELOPER = "developer"
    MAINTAINER = "maintainer"

    @property
    def rank(self) -> int:
        """Position in the ladder; higher includes everything below."""
        return ACCESS_ROLE_ORDER.index(self)


ACCESS_ROLE_ORDER = (AccessRole.VIEWER, AccessRole.DEVELOPER, AccessRole.MAINTAINER)


class ContainerPermission(StrEnum):
    """What a grant on one container service allows (and nothing else)."""

    LOGS = "logs"
    TERMINAL = "terminal"
    ACTIONS = "actions"  # start, stop, restart


class Action(StrEnum):
    """Everything a principal can try to do; routers and MCP tools name one of these."""

    PROJECT_LIST = "project.list"
    PROJECT_READ = "project.read"
    PROJECT_CREATE = "project.create"
    PROJECT_UPDATE = "project.update"
    ENVIRONMENT_UPDATE = "environment.update"
    ENVIRONMENT_POWER_READ = "environment.power_read"
    ENVIRONMENT_START = "environment.start"
    ENVIRONMENT_STOP = "environment.stop"
    RUN_LIST = "run.list"
    RUN_READ = "run.read"
    RUN_CREATE = "run.create"
    RUN_CANCEL = "run.cancel"
    RUN_RETRY = "run.retry"
    PIPELINE_READ = "pipeline.read"
    PIPELINE_WRITE = "pipeline.write"
    PIPELINE_TOOLS = "pipeline.tools"
    DEPLOYMENT_READ = "deployment.read"
    DEPLOYMENT_REDEPLOY = "deployment.redeploy"
    CONTAINER_READ = "container.read"
    CONTAINER_LOGS = "container.logs"
    CONTAINER_TERMINAL = "container.terminal"
    CONTAINER_ACTION = "container.action"
    CONTAINER_ENV_UPDATE = "container.env_update"
    CONTAINER_LIMITS = "container.limits"
    CONTAINER_FILES = "container.files"
    CONTAINER_ENV_REVEAL = "container.env_reveal"
    DATABASE_READ = "database.read"
    DATABASE_DATA = "database.data"
    DATABASE_CONFIGURE = "database.configure"
    DOMAIN_READ = "domain.read"
    DOMAIN_MANAGE = "domain.manage"
    DOMAIN_DISCOVER = "domain.discover"
    DOMAIN_DISCOVER_REFRESH = "domain.discover_refresh"
    DOMAIN_LOOKUP = "domain.lookup"
    DOMAIN_ADOPT = "domain.adopt"
    DOMAIN_ADOPT_ALL = "domain.adopt_all"
    CERTIFICATE_LIST = "certificate.list"
    SECRET_LIST = "secret.list"  # noqa: S105 - action name, not a credential
    SECRET_WRITE = "secret.write"  # noqa: S105 - action name, not a credential
    SECRET_REVEAL = "secret.reveal"  # noqa: S105 - action name, not a credential
    TELEMETRY_READ = "telemetry.read"
    TOKEN_MANAGE = "token.manage"  # noqa: S105 - action name, not a credential
    USER_MANAGE = "user.manage"
    ACCESS_MANAGE = "access.manage"
    INTEGRATION_MANAGE = "integration.manage"
    MCP_TOOLS = "mcp.tools"
    NOTIFICATION_POLICY = "notification.policy"
    NOTIFICATION_MANAGE = "notification.manage"
    INGRESS_READINESS = "ingress.readiness"
    ACCOUNT_MANAGE = "account.manage"
    AUDIT_READ = "audit.read"
    DASHBOARD_READ = "dashboard.read"


class RuleKind(StrEnum):
    """How a rule is satisfied by a ``member``."""

    GLOBAL_ADMIN = "global_admin"  # never; only owners and admins
    ANY_USER = "any_user"  # every authenticated user
    ANY_GRANT = "any_grant"  # a member holding at least one grant
    VISIBLE = "visible"  # a member that has any grant touching the resource
    ROLE = "role"  # a minimum role on the resource (and optionally a container permission)


@dataclass(frozen=True, slots=True)
class Rule:
    """The requirement of one action.

    ``development``/``production`` are the minimum roles for the two environments (production
    can demand more). ``service`` is the container permission that satisfies the action for a
    grant on exactly that service (``any_service`` means any permission on the service).
    ``scope`` is the API-token scope needed (``production_scope`` overrides it for production).
    A ``None`` scope means a browser session only (a token can never perform the action).
    """

    kind: RuleKind
    scope: TokenScope | None
    development: AccessRole = AccessRole.VIEWER
    production: AccessRole | None = None
    service: ContainerPermission | None = None
    any_service: bool = False
    production_scope: TokenScope | None = None

    def minimum(self, environment: EnvironmentName | None) -> AccessRole:
        """The role needed on ``environment`` (the stricter production role when relevant)."""
        if environment is EnvironmentName.PRODUCTION and self.production is not None:
            return self.production
        if environment is None and self.production is not None:
            return self.production  # a project-wide action must hold for production too
        return self.development

    def scope_for(self, environment: EnvironmentName | None) -> TokenScope | None:
        """The token scope for ``environment``."""
        if environment is EnvironmentName.PRODUCTION and self.production_scope is not None:
            return self.production_scope
        return self.scope


_V, _D, _M = AccessRole.VIEWER, AccessRole.DEVELOPER, AccessRole.MAINTAINER
_S = TokenScope


def _role(  # noqa: PLR0913 - one value per rule field
    scope: TokenScope,
    development: AccessRole,
    production: AccessRole | None = None,
    *,
    service: ContainerPermission | None = None,
    any_service: bool = False,
    production_scope: TokenScope | None = None,
) -> Rule:
    return Rule(
        RuleKind.ROLE, scope, development, production, service, any_service, production_scope
    )


RULES: dict[Action, Rule] = {
    Action.PROJECT_LIST: Rule(RuleKind.ANY_USER, _S.READ),
    Action.PROJECT_READ: Rule(RuleKind.VISIBLE, _S.READ),
    Action.PROJECT_CREATE: Rule(RuleKind.GLOBAL_ADMIN, _S.ADMIN),
    Action.PROJECT_UPDATE: Rule(RuleKind.GLOBAL_ADMIN, _S.ADMIN),
    Action.ENVIRONMENT_UPDATE: Rule(RuleKind.GLOBAL_ADMIN, _S.ADMIN),
    Action.ENVIRONMENT_POWER_READ: _role(_S.READ, _V),
    Action.ENVIRONMENT_START: _role(_S.DEPLOY, _D, _M),
    Action.ENVIRONMENT_STOP: _role(_S.DEPLOY, _D, _M),
    Action.RUN_LIST: Rule(RuleKind.ANY_USER, _S.READ),
    Action.RUN_READ: _role(_S.READ, _V),
    Action.RUN_CREATE: _role(_S.RUN, _D, _M),
    Action.RUN_CANCEL: _role(_S.RUN, _D, _M),
    Action.RUN_RETRY: _role(_S.RUN, _D, _M),
    Action.PIPELINE_READ: _role(_S.READ, _V),
    Action.PIPELINE_WRITE: _role(_S.PIPELINES, _D, _M),
    Action.PIPELINE_TOOLS: Rule(RuleKind.ANY_USER, _S.READ),
    Action.DEPLOYMENT_READ: _role(_S.READ, _V),
    Action.DEPLOYMENT_REDEPLOY: _role(_S.DEPLOY, _M),
    Action.CONTAINER_READ: _role(_S.READ, _V, any_service=True),
    Action.CONTAINER_LOGS: _role(_S.CONTAINERS, _M, service=ContainerPermission.LOGS),
    Action.CONTAINER_TERMINAL: _role(_S.TERMINAL, _M, service=ContainerPermission.TERMINAL),
    Action.CONTAINER_ACTION: _role(_S.CONTAINERS, _M, service=ContainerPermission.ACTIONS),
    Action.CONTAINER_ENV_UPDATE: _role(_S.CONTAINERS, _M),
    Action.CONTAINER_LIMITS: _role(_S.CONTAINERS, _M),
    Action.CONTAINER_FILES: _role(_S.CONTAINERS, _M),
    Action.CONTAINER_ENV_REVEAL: Rule(RuleKind.GLOBAL_ADMIN, _S.ADMIN),
    Action.DATABASE_READ: _role(_S.READ, _V),
    Action.DATABASE_DATA: _role(_S.READ, _D, _M, production_scope=_S.ADMIN),
    Action.DATABASE_CONFIGURE: Rule(RuleKind.GLOBAL_ADMIN, _S.ADMIN),
    Action.DOMAIN_READ: _role(_S.READ, _V),
    Action.DOMAIN_MANAGE: _role(_S.ADMIN, _M),
    # Everything Cloudflare and Apache already serve, across all projects: owners and admins.
    Action.DOMAIN_DISCOVER: Rule(RuleKind.GLOBAL_ADMIN, _S.READ),
    Action.DOMAIN_DISCOVER_REFRESH: Rule(RuleKind.GLOBAL_ADMIN, _S.ADMIN),
    # Zone list and "does this name exist" for the form of one environment's domains.
    Action.DOMAIN_LOOKUP: _role(_S.ADMIN, _M),
    # Recording an existing domain: a maintainer may do it for their own project; adopting every
    # suggestion at once spans projects, so it is for owners and admins.
    Action.DOMAIN_ADOPT: _role(_S.ADMIN, _M),
    Action.DOMAIN_ADOPT_ALL: Rule(RuleKind.GLOBAL_ADMIN, _S.ADMIN),
    Action.CERTIFICATE_LIST: Rule(RuleKind.GLOBAL_ADMIN, _S.READ),
    Action.SECRET_LIST: _role(_S.READ, _V),
    Action.SECRET_WRITE: Rule(RuleKind.GLOBAL_ADMIN, _S.ADMIN),
    Action.SECRET_REVEAL: Rule(RuleKind.GLOBAL_ADMIN, _S.ADMIN),
    Action.TELEMETRY_READ: Rule(RuleKind.ANY_GRANT, _S.READ),
    Action.TOKEN_MANAGE: Rule(RuleKind.ANY_USER, None),
    Action.USER_MANAGE: Rule(RuleKind.GLOBAL_ADMIN, None),
    Action.ACCESS_MANAGE: Rule(RuleKind.GLOBAL_ADMIN, None),
    Action.INTEGRATION_MANAGE: Rule(RuleKind.GLOBAL_ADMIN, None),
    Action.MCP_TOOLS: Rule(RuleKind.ANY_USER, None),
    Action.NOTIFICATION_POLICY: Rule(RuleKind.ANY_USER, _S.READ),
    Action.NOTIFICATION_MANAGE: Rule(RuleKind.GLOBAL_ADMIN, _S.ADMIN),
    Action.INGRESS_READINESS: Rule(RuleKind.ANY_USER, _S.READ),
    # The caller's own account (profile, sessions, two-factor, security log): sessions only.
    Action.ACCOUNT_MANAGE: Rule(RuleKind.ANY_USER, None),
    # The audit log: sessions only. Owners and admins see everything, a member only their own
    # activity on what they can still see (the scope is applied by ``authz.audit_scope``).
    Action.AUDIT_READ: Rule(RuleKind.ANY_USER, None),
    # The home dashboard: every card is filtered to what the caller may see.
    Action.DASHBOARD_READ: Rule(RuleKind.ANY_USER, _S.READ),
}


@dataclass(frozen=True, slots=True)
class Resource:
    """What an action targets: nothing (global), a project, an environment or a service."""

    project: str | None = None
    environment: EnvironmentName | None = None
    service: str | None = None

    @classmethod
    def of_project(cls, project: str) -> Resource:
        """A project as a whole."""
        return cls(project)

    @classmethod
    def of_environment(cls, project: str, environment: EnvironmentName) -> Resource:
        """One environment of a project."""
        return cls(project, environment)

    @classmethod
    def of_service(cls, project: str, environment: EnvironmentName, service: str) -> Resource:
        """One compose service of an environment."""
        return cls(project, environment, service)


GLOBAL = Resource()
# A service name no grant can carry: asks "does the role on the environment alone allow this?".
NO_SERVICE = ""


@dataclass(frozen=True, slots=True)
class Grant:
    """One access grant of a user.

    A project grant has neither ``environment`` nor ``service``; an environment grant has an
    ``environment``; a service grant has both ``environment`` and ``service``. Project and
    environment grants carry a ``role``; service grants carry ``permissions``.
    """

    project: str
    environment: EnvironmentName | None
    service: str | None
    role: AccessRole | None
    permissions: frozenset[ContainerPermission] = frozenset()
