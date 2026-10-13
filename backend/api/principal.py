"""The authenticated caller of a request: a browser session or an API token."""

from __future__ import annotations

from dataclasses import dataclass

from backend.domain.access import GlobalRole, Grant
from backend.domain.actor import Actor
from backend.domain.enums import ActorType, TokenScope
from backend.services.run_triggers import RunOrigin
from backend.services.sessions import SessionContext
from backend.services.tokens import AuthenticatedToken


@dataclass(frozen=True, slots=True)
class Principal:
    """Exactly one of ``session`` or ``token`` is set."""

    session: SessionContext | None = None
    session_token: str | None = None
    token: AuthenticatedToken | None = None
    grants: tuple[Grant, ...] = ()

    @property
    def user_id(self) -> int:
        """The acting user: the session's user, or the owner of the token."""
        if self.session is not None:
            return self.session.user_id
        if self.token is not None:
            return self.token.owner_id
        raise RuntimeError("principal has neither session nor token")

    @property
    def role(self) -> GlobalRole:
        """The acting user's global role (a token acts with its owner's role)."""
        if self.session is not None:
            return self.session.role
        if self.token is not None:
            return self.token.owner_role
        raise RuntimeError("principal has neither session nor token")

    @property
    def is_session(self) -> bool:
        """Whether the caller authenticated with a browser session."""
        return self.session is not None

    @property
    def actor(self) -> Actor:
        """The actor recorded in audit events."""
        if self.session is not None:
            return Actor(ActorType.USER, str(self.session.user_id))
        if self.token is not None:
            return Actor(ActorType.TOKEN, str(self.token.id))
        raise RuntimeError("principal has neither session nor token")

    def has_scope(self, scope: TokenScope) -> bool:
        """Sessions hold every scope; ``admin`` tokens hold every scope too.

        Scopes only narrow: what the user may do is decided by ``services.policy``.
        """
        if self.token is None:
            return True
        return scope in self.token.scopes or TokenScope.ADMIN in self.token.scopes

    def run_origin(self) -> RunOrigin:
        """Who a run started by this principal is attributed to (user id or token id)."""
        if self.session is not None:
            return RunOrigin(self.actor, user_id=self.session.user_id)
        if self.token is not None:
            return RunOrigin(self.actor, token_id=self.token.id)
        raise RuntimeError("principal has neither session nor token")
