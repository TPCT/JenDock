"""Schemas of the signed-in user's account endpoints (``/api/me``)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from backend.services.profile import ProfileView, SessionView
from backend.services.two_factor import Enrollment, TwoFactorStatus


class ProfileResponse(BaseModel):
    """The account."""

    id: int
    username: str
    display_name: str
    email: str | None
    created_at: datetime
    two_factor_enabled: bool

    @classmethod
    def from_view(cls, view: ProfileView) -> ProfileResponse:
        """Build from the service view."""
        return cls(
            id=view.id,
            username=view.username,
            display_name=view.display_name,
            email=view.email,
            created_at=view.created_at,
            two_factor_enabled=view.two_factor_enabled,
        )


class UpdateProfileRequest(BaseModel):
    """Body of ``PATCH /api/me``; both fields replace the stored value (empty clears it)."""

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=254)


class SessionsEndedResponse(BaseModel):
    """How many other sessions an operation ended."""

    sessions_ended: int


class SessionResponseItem(BaseModel):
    """One live session of the user."""

    id: int
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    ip_address: str | None
    user_agent: str | None
    client: str
    current: bool

    @classmethod
    def from_view(cls, view: SessionView) -> SessionResponseItem:
        """Build from the service view."""
        return cls(
            id=view.id,
            created_at=view.created_at,
            last_seen_at=view.last_seen_at,
            expires_at=view.expires_at,
            ip_address=view.ip_address,
            user_agent=view.user_agent,
            client=view.client,
            current=view.current,
        )


class TwoFactorStatusResponse(BaseModel):
    """Whether two-factor is on."""

    enabled: bool
    enabled_at: datetime | None
    recovery_codes_left: int

    @classmethod
    def from_status(cls, status: TwoFactorStatus) -> TwoFactorStatusResponse:
        """Build from the service value."""
        return cls(
            enabled=status.enabled,
            enabled_at=status.enabled_at,
            recovery_codes_left=status.recovery_codes_left,
        )


class EnrollmentResponse(BaseModel):
    """The pending key; shown once. ``uri`` is rendered as a QR code in the browser."""

    secret: str
    uri: str

    @classmethod
    def from_enrollment(cls, enrollment: Enrollment) -> EnrollmentResponse:
        """Build from the service value."""
        return cls(secret=enrollment.secret, uri=enrollment.uri)


class CodeRequest(BaseModel):
    """A one-time code from the authenticator app (or a recovery code)."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=32, repr=False)


class DisableTwoFactorRequest(CodeRequest):
    """Body of ``POST /api/me/two-factor/disable``: password and code."""

    password: str = Field(min_length=1, max_length=1024, repr=False)


class RecoveryCodesResponse(BaseModel):
    """The ten recovery codes, returned only by this call."""

    recovery_codes: list[str]
