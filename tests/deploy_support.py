"""Fakes for deployment tests: an in-memory ledger, a manual clock and a docker simulator."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from backend.adapters.container_runtime import CancelSignal, ExecResult, LineSink
from backend.adapters.deploy_health import Clock
from backend.adapters.deploy_ledger import ServiceChange
from backend.domain.enums import DeploymentStatus


@dataclass(slots=True)
class LedgerRow:
    """One recorded deployment."""

    id: int
    run_id: int
    status: DeploymentStatus = DeploymentStatus.DEPLOYING
    changes: tuple[ServiceChange, ...] = ()
    message: str | None = None


class MemoryLedger:
    """``DeploymentLedger`` that keeps rows in a dict."""

    def __init__(self) -> None:
        self.rows: dict[int, LedgerRow] = {}

    def begin(self, project: str, environment: str, run_id: int, commit_sha: str) -> int:
        """Add a row."""
        row = LedgerRow(len(self.rows) + 1, run_id)
        self.rows[row.id] = row
        return row.id

    def record_services(self, deployment_id: int, changes: Sequence[ServiceChange]) -> None:
        """Store the changes."""
        self.rows[deployment_id].changes = tuple(changes)

    def finish(self, deployment_id: int, status: DeploymentStatus, message: str | None) -> None:
        """Set the final status."""
        self.rows[deployment_id].status = status
        self.rows[deployment_id].message = message

    def fail_unfinished(self, message: str) -> list[int]:
        """Fail rows that are still deploying."""
        hit = [r.id for r in self.rows.values() if r.status is DeploymentStatus.DEPLOYING]
        for deployment_id in hit:
            self.finish(deployment_id, DeploymentStatus.FAILED, message)
        return hit

    def protected_images(self, project: str, environment: str) -> frozenset[str]:
        """Images of the newest row."""
        if not self.rows:
            return frozenset()
        newest = self.rows[max(self.rows)]
        values = [v for c in newest.changes for v in c.to_json().values() if v]
        return frozenset(values)

    @property
    def last(self) -> LedgerRow:
        """The newest row."""
        return self.rows[max(self.rows)]


class ManualClock(Clock):
    """Time that only moves when something sleeps."""

    def __init__(self) -> None:
        self.current = 0.0

    def now(self) -> float:
        """The simulated time."""
        return self.current

    def sleep(self, seconds: float) -> None:
        """Advance without waiting."""
        self.current += max(seconds, 0.01)


class NotCancelled:
    """A cancel signal that never fires."""

    def is_set(self) -> bool:
        """Never."""
        return False


@dataclass(slots=True)
class FakeContainer:
    """A simulated container."""

    id: str
    image_ref: str
    image_id: str
    status: str = "running"
    health: str | None = None
    restarts: int = 0
    exit_code: int = 0


@dataclass(slots=True)
class SimImage:
    """A simulated image."""

    id: str
    tags: list[str]
    created: str
    labels: dict[str, str] = field(default_factory=dict)


class ScriptedDocker:
    """A tiny docker and compose simulator implementing ``CommandRunner``.

    ``behaviour`` maps an image reference to what a container started from it does:
    ``healthy`` (default), ``unhealthy``, ``exited`` or ``restarting``.
    """

    def __init__(self, services: Mapping[str, str]) -> None:
        self.defined = set(services)
        self.base_images = dict(services)
        self.images: dict[str, SimImage] = {}
        self.containers: dict[str, FakeContainer] = {}
        self.behaviour: dict[str, str] = {}
        self.calls: list[list[str]] = []
        self.up_services: list[list[str]] = []
        self.removed_tags: list[str] = []
        self.up_exit_code = 0
        self._counter = 0

    # --- setup helpers ------------------------------------------------------------

    def add_image(
        self, tag: str, labels: Mapping[str, str] | None = None, created: str = ""
    ) -> str:
        """Create an image and return its id."""
        self._counter += 1
        image_id = f"sha256:{self._counter:064x}"
        stamp = created or f"2026-10-13T10:00:{self._counter:02d}Z"
        self.images[image_id] = SimImage(image_id, [tag], stamp, dict(labels or {}))
        return image_id

    def start(self, service: str, ref: str, health: str | None = None) -> FakeContainer:
        """Start a container of ``service`` from image ``ref`` as if compose had."""
        self.containers.pop(service, None)
        self._counter += 1
        container = FakeContainer(
            f"c{self._counter:063x}", ref, self.image_id(ref) or "", "running"
        )
        container.health = health
        self.containers[service] = container
        return container

    def image_id(self, ref: str) -> str | None:
        """Resolve a tag or id."""
        if ref in self.images:
            return ref
        return next((i.id for i in self.images.values() if ref in i.tags), None)

    # --- CommandRunner ------------------------------------------------------------

    def capture(  # noqa: PLR0911 - one branch per simulated command
        self, args: Sequence[str], timeout: float = 60.0
    ) -> subprocess.CompletedProcess[str]:
        """Answer a query."""
        argv = list(args)
        self.calls.append(argv)
        if argv[0] == "compose":
            return self._compose_query(argv)
        if argv[:2] == ["image", "inspect"]:
            return self._image_inspect(argv)
        if argv[:2] == ["image", "ls"]:
            ids = [i.id for i in self.images.values() if self._matches(i, argv)]
            return _done("\n".join(ids))
        if argv[0] == "inspect":
            return self._container_inspect(argv[3:])
        if argv[0] == "ps":
            return _done("\n".join(c.id for c in self.containers.values()))
        if argv[0] == "rmi":
            return self._rmi(argv[1])
        return _done("", 1, f"unsupported: {argv}")

    def stream(
        self,
        args: Sequence[str],
        sink: LineSink,
        *,
        timeout: float | None,
        cancel: CancelSignal | None = None,
    ) -> ExecResult:
        """Run ``up`` or ``rm``."""
        argv = list(args)
        self.calls.append(argv)
        if "up" in argv:
            return self._up(argv, sink)
        if "rm" in argv:
            self.containers.pop(argv[-1], None)
            return ExecResult(0)
        return ExecResult(1)

    # --- internals ----------------------------------------------------------------

    @staticmethod
    def _matches(image: SimImage, argv: list[str]) -> bool:
        wanted = [argv[i + 1].removeprefix("label=") for i, a in enumerate(argv) if a == "--filter"]
        return all(
            key in image.labels and image.labels[key] == value
            for key, _, value in (w.partition("=") for w in wanted)
        )

    def _compose_query(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        if "config" in argv:
            return _done("\n".join(sorted(self.defined)))
        if "ps" in argv:
            container = self.containers.get(argv[-1])
            return _done(container.id if container else "")
        return _done("", 1, "unsupported compose query")

    def _image_inspect(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        if "--format" in argv:
            found = self.image_id(argv[-1])
            return _done(found or "", 0 if found else 1, "" if found else "No such image")
        rows = [
            {
                "Id": image.id,
                "RepoTags": image.tags,
                "Created": image.created,
                "Config": {"Labels": image.labels},
            }
            for ident in argv[2:]
            if (image := self.images.get(ident)) is not None
        ]
        return _done(json.dumps(rows))

    def _container_inspect(self, ids: list[str]) -> subprocess.CompletedProcess[str]:
        rows = [
            {
                "Id": c.id,
                "Image": c.image_id,
                "RestartCount": c.restarts,
                "Config": {"Image": c.image_ref},
                "State": {
                    "Status": c.status,
                    "ExitCode": c.exit_code,
                    **({"Health": {"Status": c.health}} if c.health else {}),
                },
            }
            for c in self.containers.values()
            if c.id in ids
        ]
        return _done(json.dumps(rows))

    def _rmi(self, tag: str) -> subprocess.CompletedProcess[str]:
        for image in list(self.images.values()):
            if tag in image.tags:
                image.tags.remove(tag)
                self.removed_tags.append(tag)
                if not image.tags:
                    del self.images[image.id]
                return _done("")
        return _done("", 1, "No such image")

    def _override(self, argv: list[str]) -> dict[str, str]:
        files = [argv[i + 1] for i, a in enumerate(argv) if a == "-f"]
        if not files or Path(files[-1]).name != "override.yml":
            return {}
        data = json.loads(Path(files[-1]).read_text())
        return {name: entry["image"] for name, entry in data["services"].items()}

    def _up(self, argv: list[str], sink: LineSink) -> ExecResult:
        services = argv[argv.index("never") + 1 :]
        self.up_services.append(services)
        if self.up_exit_code:
            sink("up failed")
            return ExecResult(self.up_exit_code)
        override = self._override(argv)
        for service in services:
            ref = override.get(service, self.base_images[service])
            behaviour = self.behaviour.get(ref, "healthy")
            container = self.start(service, ref, health="starting")
            if behaviour == "healthy":
                container.health = "healthy"
            elif behaviour == "unhealthy":
                container.health = "unhealthy"
            elif behaviour == "exited":
                container.status, container.exit_code, container.health = "exited", 1, None
            elif behaviour == "restarting":
                container.status = "restarting"
        return ExecResult(0)


def _done(out: str, code: int = 0, err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, out, err)
