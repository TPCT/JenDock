"""Image retention: keep the newest images of each logical image, remove the rest.

Only images that carry both ``jendock.project`` and ``jendock.environment`` labels of
the environment are ever considered, so foreign and unlabelled images are never touched. An
image stays when it is among the newest ``keep`` of its logical image (its repository), is used
by any container, or is named in ``protected`` (the images of the last deployment).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from backend.adapters.container_runtime import LineSink
from backend.adapters.container_state import ContainerInspector
from backend.adapters.docker_commands import CommandRunner
from backend.adapters.errors import ContainerRuntimeError

logger = logging.getLogger(__name__)
PROJECT_LABEL = "jendock.project"
ENVIRONMENT_LABEL = "jendock.environment"
_FRACTION_DIGITS = 6
_TIMESTAMP = re.compile(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?(Z|[+-]\d\d:\d\d)?")


@dataclass(frozen=True, slots=True)
class ImageInfo:
    """A local image with the facts retention needs."""

    id: str
    tags: tuple[str, ...]
    created: datetime


def repository_of(tag: str) -> str:
    """The logical image of a tag: everything before the tag part (``sc/p-e-web:abc-r3``)."""
    name, _, last = tag.rpartition(":")
    return name if name and "/" not in last else tag


def parse_created(text: str) -> datetime:
    """Parse Docker's RFC 3339 timestamp (nanosecond fractions are cut to microseconds)."""
    match = _TIMESTAMP.fullmatch(text)
    if match is None:
        return datetime.min.replace(tzinfo=UTC)
    whole, fraction, zone = match.groups()
    micro = (fraction or ".0")[: _FRACTION_DIGITS + 1]
    offset = "+00:00" if zone in (None, "Z") else zone
    try:
        return datetime.fromisoformat(f"{whole}{micro}{offset}")
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)


def plan_removals(
    images: Iterable[ImageInfo], keep: int, protected: frozenset[str]
) -> list[tuple[str, str]]:
    """``(image id, tag)`` pairs to remove: per repository everything beyond the ``keep``
    newest images that is not protected."""
    by_repository: dict[str, dict[str, ImageInfo]] = {}
    for image in images:
        for tag in image.tags:
            by_repository.setdefault(repository_of(tag), {})[image.id] = image
    removals: list[tuple[str, str]] = []
    for repository, members in sorted(by_repository.items()):
        ordered = sorted(members.values(), key=lambda i: (i.created, i.id), reverse=True)
        for image in ordered[keep:]:
            tags = [t for t in image.tags if repository_of(t) == repository]
            if image.id in protected or any(tag in protected for tag in image.tags):
                continue
            removals.extend((image.id, tag) for tag in tags)
    return removals


class ImageRetention:
    """Applies retention to the images of one environment."""

    def __init__(self, docker: CommandRunner, inspector: ContainerInspector, keep: int) -> None:
        self._docker = docker
        self._inspector = inspector
        self._keep = keep

    def prune(
        self, project: str, environment: str, protected: frozenset[str], sink: LineSink
    ) -> list[str]:
        """Remove old images; returns the removed tags. Failures to remove one are logged."""
        images = self._labelled_images(project, environment)
        in_use = self._inspector.all_container_image_ids()
        guard = protected | in_use
        removed: list[str] = []
        for _image_id, tag in plan_removals(images, self._keep, guard):
            result = self._docker.capture(["rmi", tag])
            if result.returncode == 0:
                removed.append(tag)
                sink(f"removed old image {tag}")
            else:
                logger.warning("cannot remove image", extra={"image": tag})
        return removed

    def _labelled_images(self, project: str, environment: str) -> list[ImageInfo]:
        listing = self._docker.capture(
            [
                "image",
                "ls",
                "--all",
                "--quiet",
                "--no-trunc",
                "--filter",
                f"label={PROJECT_LABEL}={project}",
                "--filter",
                f"label={ENVIRONMENT_LABEL}={environment}",
            ]
        )
        if listing.returncode != 0:
            raise ContainerRuntimeError(f"cannot list images: {listing.stderr.strip()}")
        ids = sorted(set(listing.stdout.split()))
        if not ids:
            return []
        inspected = self._docker.capture(["image", "inspect", *ids])
        if inspected.returncode != 0:
            raise ContainerRuntimeError(f"cannot inspect images: {inspected.stderr.strip()}")
        return [info for raw in json.loads(inspected.stdout) if (info := _info(raw)) is not None]


def _info(raw: object) -> ImageInfo | None:
    if not isinstance(raw, dict):
        return None
    config = raw.get("Config")
    labels = config.get("Labels") if isinstance(config, dict) else None
    if not isinstance(labels, dict) or PROJECT_LABEL not in labels:
        return None
    tags = raw.get("RepoTags")
    names = tuple(t for t in tags if isinstance(t, str)) if isinstance(tags, list) else ()
    return ImageInfo(str(raw.get("Id", "")), names, parse_created(str(raw.get("Created", ""))))
