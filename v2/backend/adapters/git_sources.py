"""Source providers per transport, and the one that picks between them."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from backend.adapters.errors import SourceError
from backend.adapters.git_access import GitAccess, SshSettings, ssh_access
from backend.adapters.sources import CheckoutRequest, LocalGitSource, SourceProvider
from backend.domain.enums import GitTransport


class GitAccessSource:
    """A ``SourceProvider`` for one ``GitAccess``; the access is checked before every use."""

    def __init__(self, access: GitAccess, git: str = "git") -> None:
        self._access = access
        self._source = LocalGitSource(access.resolve, git, access.env_for)

    def checkout(self, request: CheckoutRequest, destination: Path) -> None:
        """Clone and check out the pinned commit."""
        self._access.check(request.repository)
        self._source.checkout(request, destination)

    def read_file(self, request: CheckoutRequest, path: str) -> str | None:
        """Read one file at the pinned commit."""
        self._access.check(request.repository)
        return self._source.read_file(request, path)


class GitSshSource(GitAccessSource):
    """Clones GitHub repositories with a deploy key over ssh.github.com:443."""

    def __init__(self, settings: SshSettings, git: str = "git") -> None:
        super().__init__(ssh_access(settings), git)


class TransportRoutingSource:
    """Sends each request to the provider of the project's transport."""

    def __init__(self, providers: Mapping[GitTransport, SourceProvider]) -> None:
        self._providers = dict(providers)

    def checkout(self, request: CheckoutRequest, destination: Path) -> None:
        """Clone and check out through the request's transport."""
        self._provider(request).checkout(request, destination)

    def read_file(self, request: CheckoutRequest, path: str) -> str | None:
        """Read one file through the request's transport."""
        return self._provider(request).read_file(request, path)

    def _provider(self, request: CheckoutRequest) -> SourceProvider:
        provider = self._providers.get(request.transport)
        if provider is None:
            raise SourceError(f"no source provider for transport {request.transport.value}")
        return provider
