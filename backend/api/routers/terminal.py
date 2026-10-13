"""Terminal endpoints: a CSRF-protected ticket request and the WebSocket that redeems it."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, WebSocket
from starlette.concurrency import run_in_threadpool

from backend.api.container import AppContainer
from backend.api.deps import Container, require_scope
from backend.api.principal import Principal
from backend.api.routers.containers import ContainerPath
from backend.api.schemas.containers import TerminalTicketRequest, TerminalTicketResponse
from backend.api.terminal_bridge import (
    CLOSE_POLICY,
    CLOSE_UNAVAILABLE,
    SessionCheck,
    TerminalBridge,
)
from backend.api.terminal_tickets import PrincipalKind, TicketGrant
from backend.domain.enums import TokenScope
from backend.domain.errors import ConflictError, PermissionDeniedError
from backend.terminal.pty_process import clamp_size
from backend.terminal.server import DEFAULT_COLS, DEFAULT_ROWS

logger = logging.getLogger(__name__)
router = APIRouter(tags=["terminal"])
TerminalUser = Annotated[Principal, Depends(require_scope(TokenScope.TERMINAL))]
Target = Annotated[ContainerPath, Depends()]
WEBSOCKET_PATH = "/api/terminal/ws"
AUTH_TIMEOUT = 5.0
MAX_AUTH_BYTES = 1024


@router.post("/api/projects/{slug}/environments/{env}/containers/{container_id}/terminal/tickets")
def create_ticket(
    path: Target, body: TerminalTicketRequest, principal: TerminalUser, container: Container
) -> TerminalTicketResponse:
    """Authorize one terminal connection: returns a single-use ticket valid for 30 seconds.

    The container must belong to the environment and (for a new terminal) be running; its exact
    id is pinned into the ticket. Present the ticket as the first WebSocket message.
    """
    info = container.container_gateway().get_container(
        path.slug, path.env, path.container_id, principal.actor
    )
    if body.terminal_id is None and info.state not in {"running", "restarting"}:
        raise ConflictError("start the container to open a terminal")
    grant = _grant(container, principal, path, body.terminal_id)
    ticket = container.terminal_tickets.issue(grant)
    return TerminalTicketResponse(
        ticket=ticket,
        expires_at=grant.expires_at,
        websocket_path=WEBSOCKET_PATH,
        container_id=info.id,
    )


def _grant(
    container: AppContainer, principal: Principal, path: ContainerPath, terminal_id: str | None
) -> TicketGrant:
    if principal.session is not None:
        kind, principal_id = PrincipalKind.SESSION, principal.session.session_id
    elif principal.token is not None:
        kind, principal_id = PrincipalKind.TOKEN, principal.token.id
    else:
        raise RuntimeError("principal has neither session nor token")
    return TicketGrant(
        kind=kind,
        principal_id=principal_id,
        actor=principal.actor,
        slug=path.slug,
        env=path.env,
        container_id=path.container_id,
        terminal_id=terminal_id,
        expires_at=container.terminal_tickets.new_expiry(),
    )


@router.websocket(WEBSOCKET_PATH)
async def terminal_socket(websocket: WebSocket) -> None:
    """The terminal WebSocket.

    Handshake: the ``Origin`` must be the public origin (a missing one is allowed only for
    API-token tickets), the first text message is ``{"type": "auth", "ticket": "...",
    "rows": 24, "cols": 80}``. For a session ticket the session cookie must belong to the same
    session that requested the ticket.
    """
    container: AppContainer = websocket.app.state.container
    origin = websocket.headers.get("origin")
    if origin is not None and origin != container.settings.public_origin.rstrip("/"):
        await websocket.close(code=CLOSE_POLICY)
        return
    await websocket.accept()
    auth = await _read_auth(websocket)
    if auth is None:
        await websocket.close(code=CLOSE_POLICY, reason="authentication required")
        return
    ticket, rows, cols = auth
    grant = container.terminal_tickets.redeem(ticket)
    if grant is None:
        await websocket.close(code=CLOSE_POLICY, reason="invalid or expired ticket")
        return
    try:
        check = await run_in_threadpool(_verify_session, container, websocket, grant, origin)
    except PermissionDeniedError:
        await websocket.close(code=CLOSE_POLICY, reason="session invalid")
        return
    bridge = TerminalBridge(
        container.settings.worker_terminal_socket_path, websocket, grant, check, rows, cols
    )
    try:
        await bridge.run()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("terminal bridge failed")
        await websocket.close(code=CLOSE_UNAVAILABLE)


async def _read_auth(websocket: WebSocket) -> tuple[str, int, int] | None:
    try:
        text = await asyncio.wait_for(websocket.receive_text(), AUTH_TIMEOUT)
        if len(text) > MAX_AUTH_BYTES:
            return None
        body = json.loads(text)
    except (TimeoutError, ValueError, RuntimeError):
        return None
    if not isinstance(body, dict) or body.get("type") != "auth":
        return None
    ticket = body.get("ticket")
    rows, cols = body.get("rows", DEFAULT_ROWS), body.get("cols", DEFAULT_COLS)
    if not isinstance(ticket, str) or not isinstance(rows, int) or not isinstance(cols, int):
        return None
    rows, cols = clamp_size(rows, cols)
    return ticket, rows, cols


def _verify_session(
    container: AppContainer, websocket: WebSocket, grant: TicketGrant, origin: str | None
) -> SessionCheck | None:
    """Check that a session ticket is used by the browser session it was issued to.

    Returns the ``check(touch)`` function that keeps verifying the principal while the terminal
    is open (the session; for token tickets, which have no cookie, the token's validity and
    ``terminal`` scope). Raises ``PermissionDeniedError``
    when the cookie is missing or belongs to another session, or the ``Origin`` is absent.
    """
    if grant.kind is PrincipalKind.TOKEN:
        return _token_check(container, grant)
    cookie = websocket.cookies.get(container.cookies.name)
    if cookie is None or origin != container.settings.public_origin.rstrip("/"):
        raise PermissionDeniedError("session required")
    context = container.session_service().resolve(cookie, touch=True)
    if context is None or context.session_id != grant.principal_id:
        raise PermissionDeniedError("session invalid")

    def check(touch: bool) -> bool:
        live = container.session_service().resolve(cookie, touch=touch)
        return live is not None and live.session_id == grant.principal_id

    return check


def _token_check(container: AppContainer, grant: TicketGrant) -> SessionCheck:
    """A token's terminal lives only while the token is valid and may use terminals."""

    def check(_touch: bool) -> bool:
        current = container.token_service().current(grant.principal_id)
        scopes = current.scopes if current else frozenset()
        return TokenScope.TERMINAL in scopes or TokenScope.ADMIN in scopes

    return check
