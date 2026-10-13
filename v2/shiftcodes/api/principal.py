"""The authenticated caller of a request: a browser session or an API token."""

from __future__ import annotations

from dataclasses import dataclass

from shiftcodes.domain.actor import Actor
from shiftcodes.domain.enums import ActorType, TokenScope
from shiftcodes.services.sessions import SessionContext
from shiftcodes.services.tokens import AuthenticatedToken


@dataclass(frozen=True, slots=True)
class Principal:
    """Exactly one of ``session`` or ``token`` is set."""

    session: SessionContext | None = None
    session_token: str | None = None
    token: AuthenticatedToken | None = None

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
        """Sessions hold every scope; ``admin`` tokens hold every scope too."""
        if self.token is None:
            return True
        return scope in self.token.scopes or TokenScope.ADMIN in self.token.scopes
