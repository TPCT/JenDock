"""The deployment journal: written before anything changes, removed when the deployment ends.

A journal that is still on disk when the worker starts means the worker died in the middle of
a deployment; ``ComposeDeployer.recover`` rolls it back from what the journal records.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from shiftcodes.adapters.deploy_ledger import ServiceChange
from shiftcodes.adapters.override_store import DIRECTORY_MODE, write_atomically

logger = logging.getLogger(__name__)
JOURNAL_NAME = "journal.json"
CORRUPT_SUFFIX = ".corrupt"


@dataclass(frozen=True, slots=True)
class JournalEntry:
    """Everything needed to undo one deployment."""

    deployment_id: int
    run_id: int
    project: str
    environment: str
    compose_project: str
    compose_files: tuple[str, ...]
    previous_override: dict[str, str]
    changes: tuple[ServiceChange, ...]

    def to_json(self) -> str:
        """Serialize for the journal file."""
        return json.dumps(
            {
                "deployment_id": self.deployment_id,
                "run_id": self.run_id,
                "project": self.project,
                "environment": self.environment,
                "compose_project": self.compose_project,
                "compose_files": list(self.compose_files),
                "previous_override": self.previous_override,
                "changes": [change.to_json() for change in self.changes],
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> JournalEntry:
        """Parse a journal file; raises ``ValueError`` or ``KeyError`` when it is malformed."""
        data = json.loads(text)
        return cls(
            deployment_id=int(data["deployment_id"]),
            run_id=int(data["run_id"]),
            project=str(data["project"]),
            environment=str(data["environment"]),
            compose_project=str(data["compose_project"]),
            compose_files=tuple(str(item) for item in data["compose_files"]),
            previous_override={str(k): str(v) for k, v in data["previous_override"].items()},
            changes=tuple(ServiceChange.from_json(item) for item in data["changes"]),
        )


class DeployJournal:
    """Journal files, one per environment (an environment deploys one thing at a time)."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def path(self, compose_project: str) -> Path:
        """The journal file of an environment."""
        return self._root / compose_project / JOURNAL_NAME

    def exists(self, compose_project: str) -> bool:
        """Whether a deployment of the environment is (or was, when the worker died) in flight."""
        return self.path(compose_project).exists()

    def write(self, entry: JournalEntry) -> None:
        """Durably record ``entry`` (fsynced, atomic)."""
        self.path(entry.compose_project).parent.mkdir(
            parents=True, exist_ok=True, mode=DIRECTORY_MODE
        )
        write_atomically(self.path(entry.compose_project), entry.to_json() + "\n")

    def remove(self, compose_project: str) -> None:
        """Delete the journal: the deployment is over."""
        self.path(compose_project).unlink(missing_ok=True)

    def pending(self) -> Sequence[JournalEntry]:
        """Every readable journal; a malformed one is set aside as ``journal.json.corrupt``."""
        entries: list[JournalEntry] = []
        for path in sorted(self._root.glob(f"*/{JOURNAL_NAME}")):
            try:
                entries.append(JournalEntry.from_json(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                logger.exception("unreadable deployment journal", extra={"path": str(path)})
                path.replace(path.with_name(path.name + CORRUPT_SUFFIX))
        return entries
