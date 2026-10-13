"""The signed-in user's own account: profile, password, sessions, tokens, two-factor, activity.

Every route acts on the caller only (the user id comes from the session, never from the URL)
and requires a browser session: tokens cannot manage accounts.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response

from backend.api import authz
from backend.api.deps import Container, Target, client_ip, require
from backend.api.principal import Principal
from backend.api.schemas.audit import AuditPageResponse
from backend.api.schemas.profile import (
    CodeRequest,
    DisableTwoFactorRequest,
    EnrollmentResponse,
    ProfileResponse,
    RecoveryCodesResponse,
    SessionResponseItem,
    SessionsEndedResponse,
    TwoFactorStatusResponse,
    UpdateProfileRequest,
)
from backend.domain.access import Action
from backend.domain.audit import AuditFilter
from backend.services.audit_log import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE
from backend.services.sessions import SessionContext

router = APIRouter(prefix="/api/me", tags=["me"])
# The caller's own account: a browser session, decided by the policy like everything else.
OwnAccount = Annotated[Principal, Depends(require(Action.ACCOUNT_MANAGE, Target.NONE))]


def _session(principal: Principal) -> SessionContext:
    if principal.session is None:
        raise RuntimeError("session principal without session")
    return principal.session


@router.get("")
def get_profile(principal: OwnAccount, container: Container) -> ProfileResponse:
    """The caller's profile."""
    view = container.profile_service().get(_session(principal).user_id)
    return ProfileResponse.from_view(view)


@router.patch("")
def update_profile(
    body: UpdateProfileRequest, principal: OwnAccount, container: Container
) -> ProfileResponse:
    """Set the display name and the (optional) email."""
    view = container.profile_service().update(
        _session(principal).user_id, body.display_name, body.email, principal.actor
    )
    return ProfileResponse.from_view(view)


@router.get("/sessions")
def list_sessions(principal: OwnAccount, container: Container) -> list[SessionResponseItem]:
    """The caller's live sessions; the one making this request is marked ``current``."""
    session = _session(principal)
    views = container.profile_service().list_sessions(session.user_id, session.session_id)
    return [SessionResponseItem.from_view(view) for view in views]


@router.post("/sessions/revoke-others")
def revoke_other_sessions(principal: OwnAccount, container: Container) -> SessionsEndedResponse:
    """End every session except the current one."""
    session = _session(principal)
    ended = container.profile_service().revoke_other_sessions(
        session.user_id, session.session_id, principal.actor
    )
    return SessionsEndedResponse(sessions_ended=ended)


@router.delete("/sessions/{session_id}", status_code=204)
def revoke_session(session_id: int, principal: OwnAccount, container: Container) -> Response:
    """End one of the caller's other sessions."""
    session = _session(principal)
    container.profile_service().revoke_session(
        session.user_id, session_id, session.session_id, principal.actor
    )
    return Response(status_code=204)


@router.get("/security-log")
def security_log(
    principal: OwnAccount,
    container: Container,
    cursor: Annotated[str | None, Query(max_length=256)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
) -> AuditPageResponse:
    """What the caller did and what happened to their account, newest first."""
    criteria = authz.audit_filter(principal, AuditFilter(involving_user=principal.user_id))
    page = container.audit_log_service().search(criteria, cursor=cursor, limit=limit)
    return AuditPageResponse.from_page(page)


@router.get("/two-factor")
def two_factor_status(principal: OwnAccount, container: Container) -> TwoFactorStatusResponse:
    """Whether two-factor authentication is on, and the recovery codes left."""
    status = container.two_factor_service().status(_session(principal).user_id)
    return TwoFactorStatusResponse.from_status(status)


@router.post("/two-factor/enroll")
def begin_two_factor(principal: OwnAccount, container: Container) -> EnrollmentResponse:
    """Create a pending key; the response (key and ``otpauth://`` URI) is shown once."""
    enrollment = container.two_factor_service().begin_enrollment(_session(principal).user_id)
    return EnrollmentResponse.from_enrollment(enrollment)


@router.post("/two-factor/enable")
def enable_two_factor(
    body: CodeRequest, request: Request, principal: OwnAccount, container: Container
) -> RecoveryCodesResponse:
    """Activate two-factor with a code from the app; returns the recovery codes once."""
    codes = container.two_factor_service().confirm_enrollment(
        _session(principal).user_id, body.code, principal.actor, client_ip(request)
    )
    return RecoveryCodesResponse(recovery_codes=codes)


@router.post("/two-factor/recovery-codes")
def regenerate_recovery_codes(
    body: CodeRequest, request: Request, principal: OwnAccount, container: Container
) -> RecoveryCodesResponse:
    """Replace the recovery codes (a valid code is required); the old ones stop working."""
    codes = container.two_factor_service().regenerate_recovery_codes(
        _session(principal).user_id, body.code, principal.actor, client_ip(request)
    )
    return RecoveryCodesResponse(recovery_codes=codes)


@router.post("/two-factor/disable", status_code=204)
def disable_two_factor(
    body: DisableTwoFactorRequest, request: Request, principal: OwnAccount, container: Container
) -> Response:
    """Turn two-factor off; the password and a valid code are required."""
    container.two_factor_service().disable(
        _session(principal).user_id, body.password, body.code, principal.actor, client_ip(request)
    )
    return Response(status_code=204)
