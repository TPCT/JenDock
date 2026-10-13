"""Reporting a run's state to GitHub as a commit status (``POST /repos/{o}/{r}/statuses/{sha}``)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from shiftcodes.adapters.errors import AdapterError
from shiftcodes.domain.values import CommitSha, GitHubRepository

GITHUB_API = "https://api.github.com"
API_VERSION = "2022-11-28"
USER_AGENT = "shiftcodes"
REQUEST_TIMEOUT = 10.0
MAX_DESCRIPTION = 140  # GitHub rejects longer descriptions
MAX_CONTEXT = 255


class CommitState(StrEnum):
    """The states GitHub accepts."""

    PENDING = "pending"
    SUCCESS = "success"
    FAILURE = "failure"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class CommitStatus:
    """One status to publish."""

    repository: str
    commit_sha: str
    state: CommitState
    description: str
    context: str
    target_url: str | None = None


class HttpPoster(Protocol):
    """Sends one JSON POST and returns the HTTP status code."""

    def post_json(self, url: str, headers: Mapping[str, str], body: bytes, timeout: float) -> int:
        """POST ``body``; raise ``AdapterError`` when no response was received."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would resend the bearer token elsewhere; report it as a failure instead."""

    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


class UrllibPoster:
    """``HttpPoster`` over urllib (https only, redirects are not followed)."""

    def post_json(self, url: str, headers: Mapping[str, str], body: bytes, timeout: float) -> int:
        """POST ``body`` to ``url`` and return the status code."""
        if not url.startswith("https://"):
            raise AdapterError("commit statuses are only sent over https")
        request = urllib.request.Request(url, data=body, headers=dict(headers), method="POST")  # noqa: S310
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=timeout) as response:
                response.read(1024)
                return int(response.status)
        except urllib.error.HTTPError as error:
            return int(error.code)
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise AdapterError(f"no response from GitHub: {type(error).__name__}") from error


class GithubStatusPublisher:
    """Builds the GitHub request; the token is only ever put in the ``Authorization`` header."""

    def __init__(self, poster: HttpPoster | None = None, api_url: str = GITHUB_API) -> None:
        self._poster = poster or UrllibPoster()
        self._api_url = api_url.rstrip("/")

    def publish(self, status: CommitStatus, token: str) -> None:
        """Create the status; raise ``AdapterError`` unless GitHub answers 201."""
        repository = GitHubRepository(status.repository)
        sha = CommitSha(status.commit_sha)
        url = f"{self._api_url}/repos/{repository}/statuses/{sha}"
        payload: dict[str, str] = {
            "state": status.state.value,
            "description": status.description[:MAX_DESCRIPTION],
            "context": status.context[:MAX_CONTEXT],
        }
        if status.target_url:
            payload["target_url"] = status.target_url
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "X-GitHub-Api-Version": API_VERSION,
        }
        code = self._poster.post_json(url, headers, json.dumps(payload).encode(), REQUEST_TIMEOUT)
        if code != 201:
            raise AdapterError(f"GitHub rejected the commit status (HTTP {code})")
