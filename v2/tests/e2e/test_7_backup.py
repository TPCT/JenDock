"""Step 9: backup, restore into a fresh data directory, migrate, and a second API serves it."""

from __future__ import annotations

import time
import urllib.error
import urllib.request

import pytest

from tests.docker_support import label_args
from tests.e2e.client import Api
from tests.e2e.scenario import ENV, SLUG, Scenario
from tests.e2e.stack import ADMIN_USER, PUBLIC_ORIGIN, UID, Stack, free_port, run

pytestmark = pytest.mark.e2e
DATA = "/var/lib/shiftcodes"


def _env(stack: Stack) -> list[str]:
    values = {
        "SHIFTCODES_ENVIRONMENT": "production",
        "SHIFTCODES_DATA_DIR": DATA,
        "SHIFTCODES_PUBLIC_ORIGIN": PUBLIC_ORIGIN,
        "SHIFTCODES_SECRET_KEY_FILE": f"{DATA}/secret.key",
        "SHIFTCODES_WORKER_SOCKET_PATH": "/run/shiftcodes/worker.sock",
    }
    return [a for key, value in values.items() for a in ("-e", f"{key}={value}")]


def test_17_backup_restore_and_an_api_on_the_restored_data(
    stack: Stack, api: Api, scenario: Scenario
) -> None:
    created = stack.exec_worker(
        "python", "-m", "shiftcodes.cli", "backup", "create", "--to", "/backups", "--include-logs"
    )
    assert "backup written: /backups/shiftcodes-backup-" in created.stdout
    archives = sorted(p.name for p in stack.backups.iterdir())
    assert len(archives) == 1
    assert archives[0].endswith(".tar.gz")
    assert not any(name.endswith(".secret.key") for name in archives)  # the key stays out
    archive = archives[0]
    volume = f"{stack.project}_restored"
    run(["docker", "volume", "create", *label_args(), volume])
    common = [
        "docker",
        "run",
        "--rm",
        *label_args(),
        "--user",
        f"{UID}:{UID}",
        "-v",
        f"{volume}:{DATA}",
    ]
    restore = [
        *common,
        "-v",
        f"{stack.backups}:/backups:ro",
        *_env(stack),
        stack.worker_image,
        "python",
        "-m",
        "shiftcodes.cli",
        "backup",
        "restore",
        "--from",
        f"/backups/{archive}",
    ]
    restored = run(restore)
    assert "restored" in restored.stdout
    run(
        [
            *common,
            *_env(stack),
            stack.worker_image,
            "python",
            "-m",
            "shiftcodes.repositories.migrate",
        ]
    )
    # a restore into a data directory that now has content is refused
    again = run(restore, check=False)
    assert again.returncode != 0
    assert "refusing to restore" in again.stderr
    port = free_port()
    run(
        [
            "docker",
            "run",
            "-d",
            *label_args(),
            "--name",
            f"{stack.project}-restored-api",
            "-v",
            f"{volume}:{DATA}",
            "-p",
            f"127.0.0.1:{port}:8000",
            *_env(stack),
            "-e",
            f"SHIFTCODES_MCP_ALLOWED_HOSTS=127.0.0.1:{port}",
            stack.api_image,
        ]
    )
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=2):
                break
        except (urllib.error.URLError, OSError):
            time.sleep(1)
    else:
        raise AssertionError(run(["docker", "logs", f"{stack.project}-restored-api"]).stdout)
    restored_api = Api(f"http://127.0.0.1:{port}")
    restored_api.login(ADMIN_USER, stack.admin_password)
    projects = restored_api.get("/api/projects")
    assert [p["slug"] for p in projects] == [SLUG]
    environment = next(e for e in projects[0]["environments"] if e["name"] == "development")
    assert environment["compose_project"] == stack.app_project
    assert environment["auto_deploy"] is True
    original = api.get(f"{ENV}/deployments")
    copy = restored_api.get(f"{ENV}/deployments")
    assert copy["total"] == original["total"] == 3
    assert [d["status"] for d in copy["items"]] == [d["status"] for d in original["items"]]
    assert len(restored_api.get("/api/tokens")) == len(api.get("/api/tokens"))
