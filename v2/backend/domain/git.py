"""Git values shared between the worker and the API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class BranchHead:
    """The tip of a branch. ``message``, ``author`` and ``committed_at`` are only filled in
    when the lookup asked for details (they need the commit object, not just ``ls-remote``)."""

    branch: str
    commit_sha: str
    message: str = ""
    author: str = ""
    committed_at: datetime | None = None
