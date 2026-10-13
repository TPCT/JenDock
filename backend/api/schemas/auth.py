"""Authentication schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from backend.services.sessions import SessionContext


class LoginRequest(BaseModel):
    """Credentials submitted to ``POST /api/auth/login``."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024, repr=False)


class UserResponse(BaseModel):
    """The logged-in user."""

    id: int
    username: str


class SessionResponse(BaseModel):
    """Current session: the user, the CSRF token for mutations and the hard expiry."""

    user: UserResponse
    csrf_token: str
    expires_at: datetime

    @classmethod
    def from_context(cls, context: SessionContext) -> SessionResponse:
        """Build the response from a resolved session."""
        user = UserResponse(id=context.user_id, username=context.username)
        return cls(user=user, csrf_token=context.csrf_token, expires_at=context.expires_at)
