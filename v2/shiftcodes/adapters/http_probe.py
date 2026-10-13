"""HTTP status probe used by health checks."""

from __future__ import annotations

import urllib.error
import urllib.request
from typing import Protocol

from shiftcodes.adapters.errors import AdapterError

MAX_READ = 64 * 1024


class HttpProbe(Protocol):
    """Fetches a URL once and reports the status code."""

    def get_status(self, url: str, timeout: float) -> int:
        """Return the HTTP status; raise ``AdapterError`` when no response was received."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Report a redirect as it is instead of following it."""

    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


class UrllibProbe:
    """``HttpProbe`` over urllib; redirects are reported, not followed."""

    def get_status(self, url: str, timeout: float) -> int:
        """GET ``url`` and return its status code."""
        if not url.startswith(("http://", "https://")):
            raise AdapterError("only http and https URLs can be probed")
        opener = urllib.request.build_opener(_NoRedirect)
        request = urllib.request.Request(url, method="GET")  # noqa: S310 - scheme checked above
        try:
            with opener.open(request, timeout=timeout) as response:
                response.read(MAX_READ)
                return int(response.status)
        except urllib.error.HTTPError as error:
            return int(error.code)
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise AdapterError(f"no response from {url}: {error}") from error
