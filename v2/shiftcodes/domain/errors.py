"""Domain errors. Services raise these; the API layer maps them to responses."""

from __future__ import annotations

from collections.abc import Sequence

from shiftcodes.domain.diagnostics import Diagnostic


class DomainError(Exception):
    """Base class for all expected business-rule failures."""


class NotFoundError(DomainError):
    """The requested entity does not exist."""


class ConflictError(DomainError):
    """The operation conflicts with existing state (for example a duplicate)."""


class ValidationError(DomainError):
    """An input value violates a domain rule; may carry positioned diagnostics."""

    def __init__(self, message: str, diagnostics: Sequence[Diagnostic] = ()) -> None:
        super().__init__(message)
        self.diagnostics = tuple(diagnostics)


class ConfigurationError(DomainError):
    """The platform is misconfigured in a way that must stop startup."""


class AuthenticationError(DomainError):
    """The caller is not authenticated or presented invalid credentials."""


class PermissionDeniedError(DomainError):
    """The caller is authenticated but not allowed to perform the operation."""


class RateLimitedError(DomainError):
    """Too many attempts; the caller may retry after ``retry_after`` seconds."""

    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class WorkerError(DomainError):
    """The worker failed or answered in a way the API cannot interpret."""


class WorkerUnavailableError(WorkerError):
    """The worker could not be reached in time."""


class UpstreamError(DomainError):
    """An external system the platform manages (Cloudflare, Apache helper, ACME CA) failed.

    Unlike ``WorkerError`` the message is meant for the operator and is shown as is; whoever
    raises it must keep credentials and internal paths out of it.
    """
