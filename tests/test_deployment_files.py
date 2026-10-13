"""The production deployment files stay consistent with each other (no Docker needed)."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath

import yaml  # type: ignore[import-untyped]

from backend.config import Settings
from tests.test_preflight import pf

ROOT = Path(__file__).resolve().parent.parent
OVERLAY = ROOT / "deployment" / "compose.production.yml"
EXAMPLE = ROOT / "deployment" / "env.production.example"
VHOST = ROOT / "deployment" / "apache-deploy.example.com.conf"


def test_overlay_is_valid_yaml_for_the_two_services() -> None:
    document = yaml.safe_load(OVERLAY.read_text())
    assert set(document["services"]) == {"api", "worker"}
    api = document["services"]["api"]
    assert api["read_only"] is True
    assert api["cap_drop"] == ["ALL"]
    worker = document["services"]["worker"]
    mounts = " ".join(worker["volumes"])
    assert "/usr/local/bin/docker:ro" in mounts
    assert "cli-plugins/docker-compose" not in mounts  # the image ships the plugin
    assert ":/backups" in mounts


def test_every_variable_of_the_overlay_is_documented_in_the_env_example() -> None:
    used = set(re.findall(r"\$\{([A-Z_]+)", OVERLAY.read_text()))
    documented = {
        line.split("=", 1)[0]
        for line in EXAMPLE.read_text().splitlines()
        if "=" in line and not line.startswith("#")
    }
    assert used <= documented, used - documented


def test_env_example_satisfies_the_preflight_and_names_a_v2_host(tmp_path: Path) -> None:
    results = pf.check_env_file(EXAMPLE)
    assert [r for r in results if r.status is pf.Status.FAIL] == []
    text = EXAMPLE.read_text()
    assert "PUBLIC_ORIGIN=https://deploy.example.com" in text
    assert "deploy.example.com" in text


def test_worker_socket_path_lies_in_the_shared_volume_and_matches_the_default() -> None:
    """The api and worker only meet if the socket is inside the volume both of them mount."""

    compose = yaml.safe_load((ROOT / "compose.yml").read_text())
    default = str(Settings.model_validate({"data_dir": "/tmp/x"}).worker_socket_path)  # noqa: S108
    for name in ("api", "worker"):
        service = compose["services"][name]
        socket = service["environment"]["JENDOCK_WORKER_SOCKET_PATH"]
        mounts = [v.split(":")[1] for v in service["volumes"] if v.startswith("worker-socket:")]
        assert mounts, f"{name} does not mount the worker-socket volume"
        assert PurePosixPath(socket).parent == PurePosixPath(mounts[0])
        assert socket == default


def test_vhost_proxies_to_the_loopback_port_of_the_compose_file() -> None:
    compose = yaml.safe_load((ROOT / "compose.yml").read_text())
    ports = compose["services"]["api"]["ports"]
    assert ports == ["127.0.0.1:${API_HOST_PORT:-8000}:8000"]  # default port matches the vhost
    vhost = VHOST.read_text()
    assert "http://127.0.0.1:8000/" in vhost
    assert "ServerName deploy.example.com" in vhost
