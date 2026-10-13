"""Helpers for the tests that need a real Docker daemon (``make test-docker``).

Everything the tests create carries the label ``jendock.test=1`` and a label unique to this
pytest session, ``jendock.test.session=<uuid>``. Cleanup and ``assert_nothing_left`` only look
at the session label, so suites running in parallel (other agents, CI jobs) on one daemon never
see or delete each other's resources. Use ``session_labels()`` for every container, image, network,
volume or compose project a test creates (``label_args()`` gives ``--label`` arguments).
"""

from __future__ import annotations

import shutil
import subprocess
import uuid

TEST_LABEL = "jendock.test=1"
SESSION_ID = uuid.uuid4().hex
SESSION_LABEL = f"jendock.test.session={SESSION_ID}"
BASE_CANDIDATES = ("alpine:3.20", "alpine:3.24")


def session_labels() -> dict[str, str]:
    """The labels every resource created by a test must carry."""
    return {"jendock.test": "1", "jendock.test.session": SESSION_ID}


def label_args() -> list[str]:
    """``--label`` arguments for ``docker run|build|network create|volume create``."""
    return [arg for key, value in session_labels().items() for arg in ("--label", f"{key}={value}")]


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


def _ids(*args: str) -> list[str]:
    return sorted(set(_docker(*args, "--filter", f"label={SESSION_LABEL}").stdout.split()))


def labelled_containers() -> list[str]:
    """Ids of this session's containers (any state)."""
    return _ids("ps", "--all", "--quiet")


def labelled_images() -> list[str]:
    """Ids of this session's images."""
    return _ids("image", "ls", "--all", "--quiet")


def labelled_networks() -> list[str]:
    """Ids of this session's networks."""
    return _ids("network", "ls", "--quiet")


def labelled_volumes() -> list[str]:
    """Names of this session's volumes."""
    return _ids("volume", "ls", "--quiet")


def labelled_compose_projects() -> list[str]:
    """Compose projects of this session's containers (``com.docker.compose.project`` label)."""
    result = _docker(
        "ps",
        "--all",
        "--filter",
        f"label={SESSION_LABEL}",
        "--format",
        '{{.Label "com.docker.compose.project"}}',
    )
    return sorted({name for name in result.stdout.split() if name})


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
    """Remove every resource of this session (never another session's)."""
    containers = labelled_containers()
    if containers:
        _docker("rm", "--force", "--volumes", *containers)
    images = labelled_images()
    if images:
        _docker("rmi", "--force", *images)
    networks = labelled_networks()
    if networks:
        _docker("network", "rm", *networks)
    volumes = labelled_volumes()
    if volumes:
        _docker("volume", "rm", "--force", *volumes)


def remove_image(image: str) -> None:
    """Remove an image that the tests themselves pulled."""
    _docker("rmi", image)


def assert_nothing_left() -> None:
    """Fail when a test resource survived."""
    assert labelled_containers() == [], "test containers remain"
    assert labelled_images() == [], "test images remain"
    assert labelled_networks() == [], "test networks remain"
    assert labelled_volumes() == [], "test volumes remain"
    assert labelled_compose_projects() == [], "test compose projects remain"
