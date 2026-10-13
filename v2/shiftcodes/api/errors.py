"""One place that maps domain errors (and framework errors) to HTTP responses."""

from __future__ import annotations

from collections.abc import Sequence

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from shiftcodes.domain.diagnostics import Diagnostic
from shiftcodes.domain.errors import (
    AuthenticationError,
    ConfigurationError,
    ConflictError,
    DomainError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitedError,
    SourceUnavailableError,
    UpstreamError,
    ValidationError,
    WorkerError,
    WorkerUnavailableError,
)

# Order matters: subclasses (WorkerUnavailableError) before their bases (WorkerError).
_STATUS: tuple[tuple[type[DomainError], int, str], ...] = (
    (NotFoundError, 404, "not_found"),
    (ConflictError, 409, "conflict"),
    (ValidationError, 422, "validation_error"),
    (AuthenticationError, 401, "unauthenticated"),
    (PermissionDeniedError, 403, "forbidden"),
    (RateLimitedError, 429, "rate_limited"),
    (SourceUnavailableError, 502, "source_error"),
    (WorkerUnavailableError, 503, "worker_unavailable"),
    (WorkerError, 502, "worker_error"),
    (UpstreamError, 502, "upstream_error"),
    (ConfigurationError, 500, "configuration_error"),
)
# Messages of these categories can mention internals, so clients only get a fixed text.
_GENERIC_MESSAGES = {
    "worker_unavailable": "worker is not available",
    "worker_error": "worker failed to handle the request",
    "configuration_error": "server configuration error",
}


def error_response(
    status: int,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
    diagnostics: Sequence[Diagnostic] = (),
) -> JSONResponse:
    """Build the common error envelope; positioned diagnostics are listed when present."""
    error: dict[str, object] = {"code": code, "message": message}
    if diagnostics:
        error["diagnostics"] = [_diagnostic_body(item) for item in diagnostics]
    return JSONResponse({"error": error}, status_code=status, headers=headers)


def _diagnostic_body(item: Diagnostic) -> dict[str, object]:
    return {
        "severity": item.severity.value,
        "message": item.message,
        "line": item.span.line,
        "column": item.span.column,
    }


def _domain_error_handler(_request: Request, error: Exception) -> JSONResponse:
    for error_type, status, code in _STATUS:
        if isinstance(error, error_type):
            message = _GENERIC_MESSAGES.get(code, str(error))
            headers = None
            if isinstance(error, RateLimitedError):
                headers = {"Retry-After": str(error.retry_after)}
            diagnostics = error.diagnostics if isinstance(error, ValidationError) else ()
            return error_response(status, code, message, headers, diagnostics)
    return error_response(500, "internal_error", "internal server error")


def _request_validation_handler(_request: Request, error: Exception) -> JSONResponse:
    """Report field locations and messages only; never echo the submitted input."""
    if not isinstance(error, RequestValidationError):
        raise error
    fields = [{"loc": [str(p) for p in e["loc"]], "msg": e["msg"]} for e in error.errors()]
    body = {"error": {"code": "validation_error", "message": "invalid request", "fields": fields}}
    return JSONResponse(body, status_code=422)


def _http_exception_handler(_request: Request, error: Exception) -> JSONResponse:
    if not isinstance(error, StarletteHTTPException):
        raise error
    code = "not_found" if error.status_code == 404 else "http_error"
    return error_response(error.status_code, code, str(error.detail), dict(error.headers or {}))


def register_error_handlers(app: FastAPI) -> None:
    """Install the handlers on ``app``."""
    app.add_exception_handler(DomainError, _domain_error_handler)
    app.add_exception_handler(RequestValidationError, _request_validation_handler)
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
