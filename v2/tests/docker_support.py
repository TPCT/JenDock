"""Helpers for the tests that need a real Docker daemon (``make test-docker``).

Everything the tests create carries the label ``shiftcodes.test=1`` and is removed again;
``assert_nothing_left`` verifies that.
"""

from __future__ import annotations

import shutil
import subprocess

TEST_LABEL = "shiftcodes.test=1"
BASE_CANDIDATES = ("alpine:3.20", "alpine:3.24")


def _docker(*args: str, timeout: float = 120) -> subprocess.CompletedProcess[str]:
    docker = shutil.which("docker")
    assert docker is not None
    return subprocess.run(  # noqa: S603 - fixed argv in tests
        [docker, *args], capture_output=True, text=True, timeout=timeout, check=False
    )


def docker_available() -> bool:
    """Whether a docker CLI exists and a daemon answers."""
    if shutil.which("docker") is None:
        return False
    try:
        return _docker("version", "--format", "{{.Server.Version}}", timeout=20).returncode == 0
    except subprocess.SubprocessError:
        return False


def labelled_containers() -> list[str]:
    """Ids of all containers (any state) carrying the test label."""
    result = _docker("ps", "--all", "--quiet", "--filter", f"label={TEST_LABEL}")
    return result.stdout.split()


def labelled_images() -> list[str]:
    """Ids of all images carrying the test label."""
    result = _docker("image", "ls", "--all", "--quiet", "--filter", f"label={TEST_LABEL}")
    return sorted(set(result.stdout.split()))


def running_with_label(label: str) -> list[str]:
    """Ids of running containers carrying ``label``."""
    return _docker("ps", "--quiet", "--filter", f"label={label}").stdout.split()


def image_present(image: str) -> bool:
    """Whether ``image`` exists locally."""
    return _docker("image", "inspect", "--format", "{{.Id}}", image).returncode == 0


def pick_base_image() -> tuple[str, bool]:
    """A local alpine image to use as base; pulls ``alpine:3.20`` when none is present.

    Returns ``(image, pulled_by_us)``; an image we pulled is removed at the end.
    """
    for candidate in BASE_CANDIDATES:
        if image_present(candidate):
            return candidate, False
    pull = _docker("pull", "--quiet", BASE_CANDIDATES[0], timeout=300)
    assert pull.returncode == 0, pull.stderr
    return BASE_CANDIDATES[0], True


def remove_test_resources() -> None:
    """Remove every container and image carrying the test label."""
    containers = labelled_containers()
    if containers:
        _docker("rm", "--force", *containers)
    images = labelled_images()
    if images:
        _docker("rmi", "--force", *images)


def remove_image(image: str) -> None:
    """Remove an image that the tests themselves pulled."""
    _docker("rmi", image)


def assert_nothing_left() -> None:
    """Fail when a test resource survived."""
    assert labelled_containers() == [], "test containers remain"
    assert labelled_images() == [], "test images remain"
