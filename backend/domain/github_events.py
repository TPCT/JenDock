"""The parts of a GitHub ``push`` webhook payload the platform uses."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass

from backend.domain.errors import ValidationError
from backend.domain.values import BranchName, CommitSha, GitHubRepository

BRANCH_REF_PREFIX = "refs/heads/"
ZERO_SHA = "0" * 40
_DELIVERY_ID = re.compile(r"[A-Za-z0-9-]{8,64}")
_SIGNATURE = re.compile(r"sha256=[0-9a-f]{64}")


def validate_delivery_id(value: str | None) -> str:
    """GitHub's ``X-GitHub-Delivery`` is a GUID; anything else is rejected."""
    if value is None or not _DELIVERY_ID.fullmatch(value):
        raise ValidationError("missing or invalid X-GitHub-Delivery header")
    return value


def is_signature_format(value: str | None) -> bool:
    """Whether ``X-Hub-Signature-256`` looks like ``sha256=<64 hex>`` (checked before any HMAC)."""
    return value is not None and _SIGNATURE.fullmatch(value) is not None


@dataclass(frozen=True, slots=True)
class PushEvent:
    """A push to a branch. ``branch`` is ``None`` for tags and other refs."""

    repository: str
    branch: str | None
    after: str
    deleted: bool

    @property
    def is_branch_delete(self) -> bool:
        """Whether the push removed the branch (GitHub sends ``deleted`` and an all-zero sha)."""
        return self.deleted or self.after == ZERO_SHA


def repository_of(payload: Mapping[str, object]) -> str | None:
    """The ``owner/name`` of the payload's repository, if present and well formed."""
    repository = payload.get("repository")
    name = repository.get("full_name") if isinstance(repository, Mapping) else None
    if not isinstance(name, str):
        return None
    try:
        return str(GitHubRepository(name))
    except ValidationError:
        return None


def parse_push(payload: Mapping[str, object]) -> PushEvent:
    """Extract the push fields; raises ``ValidationError`` for a malformed payload."""
    repository = repository_of(payload)
    ref = payload.get("ref")
    after = payload.get("after")
    if repository is None or not isinstance(ref, str) or not isinstance(after, str):
        raise ValidationError("malformed push payload")
    deleted = payload.get("deleted") is True
    if after != ZERO_SHA:
        after = str(CommitSha(after))
    return PushEvent(repository, _branch_of(ref), after, deleted)


def _branch_of(ref: str) -> str | None:
    if not ref.startswith(BRANCH_REF_PREFIX):
        return None
    try:
        return str(BranchName(ref.removeprefix(BRANCH_REF_PREFIX)))
    except ValidationError:
        return None
