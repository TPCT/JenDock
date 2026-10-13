"""Re-checking a user's password for sensitive account changes."""

from __future__ import annotations

from backend.domain.errors import ValidationError
from backend.repositories.models import User
from backend.services.passwords import PasswordHasher
from backend.services.rate_limit import LoginRateLimiter

WRONG_PASSWORD = "the password is incorrect"  # noqa: S105 - a message, not a credential


def require_password(
    hasher: PasswordHasher, limiter: LoginRateLimiter, user: User, password: str, client_ip: str
) -> None:
    """Raise unless ``password`` is the user's current one.

    Wrong answers count against the same limiter as failed logins (``RateLimitedError`` once
    exhausted), so a stolen session cannot be used to guess the password. The error is a 422,
    not a 401: the session itself is fine and the page must not log the user out.
    """
    limiter.check(user.username, client_ip)
    if not hasher.verify(user.password_hash, password):
        limiter.record_failure(user.username, client_ip)
        raise ValidationError(WRONG_PASSWORD)
    limiter.reset_username(user.username)
