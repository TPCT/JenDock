"""Session cookie policy derived from the public origin."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from fastapi import Response

from shiftcodes.config import SESSION_ABSOLUTE_TIMEOUT, Settings

PRODUCTION_COOKIE_NAME = "__Host-session"
PLAIN_COOKIE_NAME = "shiftcodes_session"


@dataclass(frozen=True, slots=True)
class CookiePolicy:
    """Name and flags of the session cookie.

    Over https the cookie is ``__Host-`` prefixed and Secure (no Domain, Path=/); only a
    development setup with an http origin falls back to a plain name without Secure.
    """

    name: str
    secure: bool

    @classmethod
    def for_settings(cls, settings: Settings) -> CookiePolicy:
        """Choose the policy from the scheme of ``public_origin``."""
        secure = urlsplit(settings.public_origin).scheme == "https"
        return cls(PRODUCTION_COOKIE_NAME if secure else PLAIN_COOKIE_NAME, secure)

    def set(self, response: Response, token: str) -> None:
        """Attach the session cookie to ``response``."""
        response.set_cookie(
            self.name,
            token,
            max_age=int(SESSION_ABSOLUTE_TIMEOUT.total_seconds()),
            path="/",
            secure=self.secure,
            httponly=True,
            samesite="strict",
        )

    def delete(self, response: Response) -> None:
        """Expire the session cookie on the client."""
        response.delete_cookie(
            self.name, path="/", secure=self.secure, httponly=True, samesite="strict"
        )
