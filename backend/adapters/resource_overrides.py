"""The managed Compose override that carries the resource limits of an environment's services.

``<root>/<project>/<environment>/resource-limits.yml`` sits next to the container-management
override file (``compose_overrides.py``) and is appended to every compose invocation: the
deployer's invocations through ``LockedDeployer`` and the service recreations of container
management. The deployer's image override stays the last ``-f``.

Why service-level keys. ``cpus``, ``mem_limit``, ``memswap_limit``, ``mem_reservation`` and
``cpu_shares`` are plain Compose service attributes: ``docker compose up`` applies them on a
single Docker host without swarm, and they are the only way to set the swap limit and the CPU
weight. ``deploy.resources`` is also honoured by Compose v2, but it has no swap or weight
setting, and Compose refuses a service that sets the same limit in both styles with different
values. So that a repository that declares ``deploy.resources.limits`` keeps deploying, the file
resets that block (``!reset``, Compose 2.24 or newer) for every service whose cpus or memory
limit Jendock sets; the Jendock values replace it. A repository that sets
``deploy.resources.reservations.memory`` or ``mem_reservation`` itself must not also use a
Jendock memory reservation for the same service.

The swap limit equals the memory limit, so the memory limit is real and ``docker update`` can
move it later. The file is YAML written by hand: every value is a number and every service
name is validated, so nothing needs escaping. It holds no secrets (mode 0644).
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from backend.adapters.compose_overrides import DIR_MODE, ComposeOverrideStore, OverrideFileError
from backend.adapters.override_store import write_atomically
from backend.domain.containers import EnvironmentKey
from backend.domain.resources import ResourceLimits, validate_service_name

FILE_NAME = "resource-limits.yml"
FORMAT_VERSION = 1
_HEADER = (
    "# Managed by Jendock: resource limits of this environment's services.\n"
    "# Rendered from the database on every deploy; edit them in Jendock, not here.\n"
)


def render_limits(limits: Mapping[str, ResourceLimits]) -> str:
    """The override document for ``limits`` (services without any limit are left out)."""
    head = [_HEADER + "x-jendock-resource-limits:", f"  version: {FORMAT_VERSION}"]
    body: list[str] = []
    for name in sorted(limits):
        entry = limits[name]
        if entry.is_empty:
            continue
        body.append(f"  {json.dumps(validate_service_name(name))}:")
        body.extend(f"    {key}: {value}" for key, value in _service_keys(entry))
        if entry.cpus is not None or entry.memory_bytes is not None:
            body.append("    deploy:\n      resources:\n        limits: !reset null")
    services = ["services:", *body] if body else ["services: {}"]
    return "\n".join([*head, *services]) + "\n"


def _service_keys(entry: ResourceLimits) -> list[tuple[str, str]]:
    keys: list[tuple[str, str]] = []
    if entry.cpus is not None:
        keys.append(("cpus", f"{entry.cpus:g}"))
    if entry.memory_bytes is not None:
        keys.append(("mem_limit", str(entry.memory_bytes)))
        keys.append(("memswap_limit", str(entry.memory_swap_bytes)))
    if entry.memory_reservation_bytes is not None:
        keys.append(("mem_reservation", str(entry.memory_reservation_bytes)))
    if entry.cpu_shares is not None:
        keys.append(("cpu_shares", str(entry.cpu_shares)))
    return keys


class ResourceOverrideStore:
    """Reads the path of and writes the resource-limits override of each environment."""

    def __init__(self, root: Path) -> None:
        self._directories = ComposeOverrideStore(root)

    def path(self, key: EnvironmentKey) -> Path:
        """The override file (it may not exist)."""
        return self._directories.directory(key) / FILE_NAME

    def write(self, key: EnvironmentKey, limits: Mapping[str, ResourceLimits]) -> Path | None:
        """Replace the file atomically; with no limit left the file is removed (``None``)."""
        path = self.path(key)
        try:
            if all(entry.is_empty for entry in limits.values()):
                path.unlink(missing_ok=True)
                return None
            path.parent.mkdir(parents=True, exist_ok=True, mode=DIR_MODE)
            write_atomically(path, render_limits(limits))
        except OSError as error:
            raise OverrideFileError("cannot write the resource limits override") from error
        return path

    def files(self, key: EnvironmentKey) -> list[str]:
        """The file as a compose ``-f`` value when it exists, otherwise nothing."""
        path = self.path(key)
        return [str(path)] if path.is_file() else []
