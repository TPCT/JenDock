"""GitHub webhook endpoint. Authenticated by the HMAC signature only (no session, no token)."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from shiftcodes.api.container import AppContainer
from shiftcodes.api.deps import Container, client_ip
from shiftcodes.domain.errors import AuthenticationError, ValidationError
from shiftcodes.domain.github_events import is_signature_format, validate_delivery_id
from shiftcodes.services.rate_limit import check_webhook, record_webhook_failure
from shiftcodes.services.webhooks import WebhookOutcome, WebhookResult

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/github", tags=["github"])
MAX_BODY_BYTES = 1024 * 1024
JSON_TYPE = "application/json"
_OK_OUTCOMES = {WebhookOutcome.PONG, WebhookOutcome.DUPLICATE}


class WebhookResponse(BaseModel):
    """What a delivery led to."""

    outcome: WebhookOutcome
    runs: list[int]
    reason: str | None

    @classmethod
    def from_result(cls, result: WebhookResult) -> WebhookResponse:
        """Build from the service result."""
        return cls(outcome=result.outcome, runs=list(result.run_ids), reason=result.reason)


async def _read_body(request: Request) -> bytes:
    """Read the body, refusing more than ``MAX_BODY_BYTES`` without buffering it all."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="request body too large")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="request body too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _verify(container: AppContainer, signature: str, body: bytes, ip: str) -> None:
    """Constant-time check done by the worker, which alone holds the webhook secret."""
    if not container.github.verify_webhook(signature, body):
        record_webhook_failure(container.webhook_limiter, ip)
        logger.warning("webhook signature mismatch")
        raise AuthenticationError("invalid webhook signature")


def _process(  # noqa: PLR0913, PLR0917 - one value per header of the delivery
    container: AppContainer, body: bytes, signature: str, delivery: str | None, event: str, ip: str
) -> WebhookResult:
    _verify(container, signature, body, ip)
    delivery_id = validate_delivery_id(delivery)
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError) as error:
        raise ValidationError("the payload is not valid JSON") from error
    if not isinstance(payload, dict):
        raise ValidationError("the payload must be a JSON object")
    return container.webhook_service().handle(delivery_id, event, payload)


@router.post("/webhook")
async def webhook(request: Request, container: Container) -> JSONResponse:
    """Handle a delivery: ``ping`` answers 200, ``push`` queues runs (202) per auto-deploy."""
    ip = client_ip(request)
    check_webhook(container.webhook_limiter, ip)  # before any worker call
    signature = request.headers.get("x-hub-signature-256")
    if signature is None or not is_signature_format(signature):
        record_webhook_failure(container.webhook_limiter, ip)
        raise AuthenticationError("missing or malformed X-Hub-Signature-256 header")
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != JSON_TYPE:
        raise HTTPException(status_code=415, detail="the webhook content type must be JSON")
    body = await _read_body(request)
    event = request.headers.get("x-github-event", "")[:32]
    delivery = request.headers.get("x-github-delivery")
    result = await run_in_threadpool(_process, container, body, signature, delivery, event, ip)
    status = 200 if result.outcome in _OK_OUTCOMES else 202
    return JSONResponse(WebhookResponse.from_result(result).model_dump(mode="json"), status)
