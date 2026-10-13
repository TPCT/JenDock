"""``docker update`` of resource limits and host capacity against a real daemon.

Everything created carries the session labels of ``tests.docker_support`` and is removed again.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import pytest

from backend.adapters.container_admin import DockerContainerEngine, LiveUpdate
from backend.adapters.docker_client import DockerClient
from tests.docker_support import (
    _docker,
    docker_available,
    label_args,
    pick_base_image,
    remove_test_resources,
)

pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not docker_available(), reason="no Docker daemon available"),
]
MIB = 1024**2


@pytest.fixture
def container_id() -> Iterator[str]:
    image, _pulled = pick_base_image()
    created = _docker("run", "-d", *label_args(), image, "sleep", "120")
    assert created.returncode == 0, created.stderr
    try:
        yield created.stdout.strip()
    finally:
        remove_test_resources()


def _host_config(container_id: str) -> dict[str, int]:
    return json.loads(_docker("inspect", container_id).stdout)[0]["HostConfig"]  # type: ignore[no-any-return]


def test_update_moves_memory_with_swap_and_back_up(container_id: str) -> None:
    engine = DockerContainerEngine(DockerClient())
    engine.update_resources(
        container_id, LiveUpdate(cpus=0.5, memory_bytes=256 * MIB, memory_swap_bytes=256 * MIB)
    )
    host = _host_config(container_id)
    assert host["NanoCpus"] == 500_000_000 and host["Memory"] == host["MemorySwap"] == 256 * MIB
    # below the current swap and above it again: both work because swap travels along
    for size in (64 * MIB, 512 * MIB):
        engine.update_resources(container_id, LiveUpdate(memory_bytes=size, memory_swap_bytes=size))
        assert _host_config(container_id)["Memory"] == size


def test_host_capacity_is_what_docker_reports() -> None:
    capacity = DockerContainerEngine(DockerClient()).host_capacity()
    assert capacity.cpus >= 1 and capacity.memory_bytes > 64 * MIB
