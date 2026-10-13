"""RPC methods for GitHub: webhook signature verification and branch head lookup.

The webhook secret lives in a file of the worker-only keys volume. The API forwards the raw
body and the signature header and only learns whether the signature is valid, so the process
that parses internet input never holds the secret (see docs/github.md).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
from collections.abc import Callable, Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError

from backend.adapters.errors import SourceError
from backend.adapters.git_remote import BranchHeadReader
from backend.domain.enums import GitTransport
from backend.domain.errors import ConfigurationError, NotFoundError, SourceUnavailableError
from backend.domain.git import BranchHead
from backend.domain.github_events import is_signature_format
from backend.domain.values import BranchName
from backend.repositories.database import UnitOfWork
from backend.services.lookup import require_project
from backend.worker.rpc import BadRequestError, Handler, RpcCall

SIGNATURE_PREFIX = "sha256="
ProjectSource = Callable[[str], tuple[str, GitTransport]]


class _VerifyParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signature: str = Field(max_length=128)
    body_b64: str


class _HeadParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(max_length=64)
    branch: str = Field(max_length=200)
    details: bool = False


def _parse[P: BaseModel](model: type[P], call: RpcCall) -> P:
    try:
        return model.model_validate(call.params)
    except PydanticValidationError as error:
        # Never echo the input: it contains the webhook body.
        raise BadRequestError("invalid params") from error


def read_webhook_secret(path: Path | None) -> bytes:
    """The webhook secret (surrounding whitespace removed); an unset or empty file is fatal."""
    if path is None:
        raise ConfigurationError("the GitHub webhook secret file is not configured")
    try:
        secret = path.read_bytes().strip()
    except OSError as error:
        raise ConfigurationError("the GitHub webhook secret file cannot be read") from error
    if not secret:
        raise ConfigurationError("the GitHub webhook secret file is empty")
    return secret


def signature_matches(secret: bytes, body: bytes, signature: str) -> bool:
    """Constant-time comparison of ``signature`` with the HMAC-SHA256 of ``body``."""
    if not is_signature_format(signature):
        return False
    expected = SIGNATURE_PREFIX + hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), signature.encode())


class GithubHandlers:
    """Adapts the GitHub operations to RPC calls."""

    def __init__(
        self,
        secret_file: Path | None,
        heads: BranchHeadReader,
        projects: ProjectSource,
        on_invalid_signature: Callable[[], None] | None = None,
    ) -> None:
        self._secret_file = secret_file
        self._heads = heads
        self._projects = projects
        self._on_invalid = on_invalid_signature

    def verify_webhook(self, call: RpcCall) -> object:
        """``github.verify_webhook``: ``{"valid": bool}``."""
        params = _parse(_VerifyParams, call)
        try:
            body = base64.b64decode(params.body_b64, validate=True)
        except (binascii.Error, ValueError) as error:
            raise BadRequestError("invalid params") from error
        secret = read_webhook_secret(self._secret_file)
        valid = signature_matches(secret, body, params.signature)
        if not valid and self._on_invalid is not None:
            self._on_invalid()  # counts failures for the notification spike alert
        return {"valid": valid}

    def branch_head(self, call: RpcCall) -> object:
        """``github.branch_head``: the head of a branch of the project's repository."""
        params = _parse(_HeadParams, call)
        call.require_actor()
        branch = str(BranchName(params.branch))
        repository, transport = self._projects(params.slug)
        try:
            head = self._heads.head(repository, transport, branch, details=params.details)
        except SourceError as error:
            raise SourceUnavailableError(str(error)) from error
        if head is None:
            raise NotFoundError(f"branch not found: {branch}")
        return _head_payload(head)


def _head_payload(head: BranchHead) -> dict[str, object]:
    return {
        "branch": head.branch,
        "commit_sha": head.commit_sha,
        "message": head.message,
        "author": head.author,
        "committed_at": head.committed_at.isoformat() if head.committed_at else None,
    }


def project_source(uow_factory: Callable[[], UnitOfWork]) -> ProjectSource:
    """Look up the repository and transport of a project by slug."""

    def lookup(slug: str) -> tuple[str, GitTransport]:
        with uow_factory() as uow:
            project = require_project(uow, slug)
            return project.repository, project.git_transport

    return lookup


def github_handlers(
    secret_file: Path | None,
    heads: BranchHeadReader,
    projects: ProjectSource,
    on_invalid_signature: Callable[[], None] | None = None,
) -> Mapping[str, Handler]:
    """The GitHub entries of the RPC allowlist."""
    handlers = GithubHandlers(secret_file, heads, projects, on_invalid_signature)
    return {
        "github.verify_webhook": handlers.verify_webhook,
        "github.branch_head": handlers.branch_head,
    }
