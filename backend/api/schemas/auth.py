"""Authentication schemas."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from backend.api.principal import Principal
from backend.domain.access import GlobalRole
from backend.services import policy


class LoginRequest(BaseModel):
    """Credentials submitted to ``POST /api/auth/login``."""

    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024, repr=False)


class ChangePasswordRequest(BaseModel):
    """Body of ``POST /api/auth/change-password``."""

    model_config = ConfigDict(extra="forbid")

    current_password: str = Field(min_length=1, max_length=1024, repr=False)
    new_password: str = Field(min_length=1, max_length=1024, repr=False)


class TwoFactorLoginRequest(BaseModel):
    """Second login step: the challenge from ``/login`` and a code."""

    model_config = ConfigDict(extra="forbid")

    challenge: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=1, max_length=32, repr=False)


class MfaRequiredResponse(BaseModel):
    """The password was right but a second factor is needed; no session exists yet."""

    mfa_required: bool = True
    challenge: str


class UserResponse(BaseModel):
    """The logged-in user."""

    id: int
    username: str
    display_name: str = ""
    email: str | None = None
    role: GlobalRole


class SessionResponse(BaseModel):
    """Current session: the user, the CSRF token for mutations, the hard expiry and what the
    user may do.

    ``permissions`` is the effective summary (see ``services.policy.summarize``) the UI uses to
    hide actions; the API stays the authority. ``must_change_password`` is true after an admin
    reset: every endpoint except this one, logout and change-password answers 403
    ``password_change_required`` until the password is changed.
    """

    user: UserResponse
    csrf_token: str
    expires_at: datetime
    must_change_password: bool = False
    permissions: dict[str, object] = Field(default_factory=dict)

    @classmethod
    def from_principal(cls, principal: Principal) -> SessionResponse:
        """Build the response from an authenticated browser session."""
        context = principal.session
        if context is None:
            raise RuntimeError("principal without session")
        user = UserResponse(
            id=context.user_id,
            username=context.username,
            display_name=context.display_name,
            email=context.email,
            role=context.role,
        )
        return cls(
            user=user,
            csrf_token=context.csrf_token,
            expires_at=context.expires_at,
            must_change_password=context.must_change_password,
            permissions=policy.summarize(principal),
        )
