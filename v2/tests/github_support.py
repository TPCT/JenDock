"""Fakes and builders for GitHub tests: a signing helper, push payloads and a fake gateway."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping

from shiftcodes.domain.actor import Actor
from shiftcodes.domain.errors import NotFoundError
from shiftcodes.domain.git import BranchHead
from shiftcodes.worker.github_handlers import signature_matches

SECRET = b"webhook-secret-for-tests"
ZERO = "0" * 40
SHA_A = "a" * 40
SHA_B = "b" * 40
DELIVERY = "8f2c6c1e-5a0d-11ef-9b3a-0242ac110002"


def sign(body: bytes, secret: bytes = SECRET) -> str:
    """The ``X-Hub-Signature-256`` value GitHub would send."""
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def push_payload(
    ref: str = "refs/heads/development",
    after: str = SHA_A,
    repository: str = "octo/demo",
    *,
    deleted: bool = False,
) -> bytes:
    """A minimal ``push`` payload."""
    body: Mapping[str, object] = {
        "ref": ref,
        "after": after,
        "deleted": deleted,
        "repository": {"full_name": repository},
    }
    return json.dumps(body).encode()


def webhook_headers(
    body: bytes,
    *,
    event: str = "push",
    delivery: str | None = DELIVERY,
    signature: str | None = None,
    content_type: str = "application/json",
) -> dict[str, str]:
    """Headers of a GitHub delivery (signed unless ``signature`` is given)."""
    headers = {
        "Content-Type": content_type,
        "X-GitHub-Event": event,
        "X-Hub-Signature-256": signature if signature is not None else sign(body),
    }
    if delivery is not None:
        headers["X-GitHub-Delivery"] = delivery
    return headers


class FakeGithub:
    """A ``GithubGateway`` with a fixed secret and scripted branch heads."""

    def __init__(self, heads: Mapping[str, str] | None = None) -> None:
        self.heads: dict[str, str] = dict(heads or {})
        self.lookups: list[tuple[str, str, bool]] = []

    def verify_webhook(self, signature: str, body: bytes) -> bool:
        """Same check as the worker."""
        return signature_matches(SECRET, body, signature)

    def branch_head(
        self, slug: str, branch: str, actor: Actor, *, details: bool = False
    ) -> BranchHead:
        """The scripted head of ``branch``."""
        self.lookups.append((slug, branch, details))
        sha = self.heads.get(branch)
        if sha is None:
            raise NotFoundError(f"branch not found: {branch}")
        if not details:
            return BranchHead(branch, sha)
        return BranchHead(branch, sha, "Fix the thing", "Ada", None)
