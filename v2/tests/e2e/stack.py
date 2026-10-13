"""The real stack of ``compose.yml`` for the end-to-end test.

Everything is created for one test session: a unique compose project (``jdtest-e2e-<id>``),
temporary host directories for the workspaces, the sample projects, the git repositories and
the backups, and a docker network the sample project shares with the worker. Every container,
image, volume and network carries the session labels of ``tests.docker_support`` so teardown
removes exactly this session's resources and never anything else on the daemon.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from tests.docker_support import remove_test_resources, session_labels

V2_ROOT = Path(__file__).resolve().parent.parent.parent
ADMIN_USER = "e2e-admin"
PUBLIC_ORIGIN = "https://e2e.test"
SOCKET = Path("/var/run/docker.sock")
UID = 10001
BUILD_TIMEOUT = 1500
COMMAND_TIMEOUT = 300


def free_port() -> int:
    """An unused loopback TCP port (the usual small race between probe and use applies)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class CommandError(AssertionError):
    """A docker command that had to succeed did not."""


def run(
    argv: Sequence[str],
    *,
    input_text: str | None = None,
    timeout: float = COMMAND_TIMEOUT,
    env: Mapping[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a command (never through a shell); raises ``CommandError`` on failure."""
    done = subprocess.run(  # noqa: S603 - explicit argv of a test harness
        list(argv),
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=dict(os.environ, **(env or {})),
        check=False,
    )
    if check and done.returncode != 0:
        raise CommandError(
            f"{' '.join(argv[:6])} failed ({done.returncode}): {done.stderr[-2000:]}"
        )
    return done


def label_lines(indent: int) -> str:
    """The session labels as a YAML mapping body at ``indent`` spaces."""
    pad = " " * indent
    return "\n".join(f'{pad}"{key}": "{value}"' for key, value in session_labels().items())


@dataclass(slots=True)
class Stack:
    """The platform under test; ``start`` builds and starts it, ``stop`` removes everything."""

    root: Path
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    api_port: int = field(default_factory=free_port)
    web_port: int = field(default_factory=free_port)
    timings: dict[str, float] = field(default_factory=dict)
    admin_password: str = field(default_factory=lambda: f"pw-{uuid.uuid4().hex}")
    webhook_secret: str = field(default_factory=lambda: uuid.uuid4().hex)
    db_password: str = field(default_factory=lambda: uuid.uuid4().hex)

    # --- names and directories ----------------------------------------------------

    @property
    def project(self) -> str:
        return f"jdtest-e2e-{self.id}"

    @property
    def app_project(self) -> str:
        """The compose project of the deployed sample application."""
        return f"jdtest-e2e-{self.id}-app"

    @property
    def apps_dir(self) -> Path:
        return self.root / "projects"

    @property
    def workspaces(self) -> Path:
        return self.root / "workspaces"

    @property
    def repos(self) -> Path:
        return self.root / "repos"

    @property
    def backups(self) -> Path:
        return self.root / "backups"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.api_port}"

    @property
    def worker_image(self) -> str:
        return f"{self.project}-worker"

    @property
    def api_image(self) -> str:
        return f"{self.project}-api"

    # --- compose ------------------------------------------------------------------

    @property
    def override_file(self) -> Path:
        return self.root / "compose.e2e.yml"

    def write_override(self) -> None:
        """Ports, origin, git template, labels and extra mounts for the test run."""
        labels = label_lines(8)
        env_labels = label_lines(6)
        self.override_file.write_text(
            f"""services:
  api:
    ports: !override
      - "127.0.0.1:{self.api_port}:8000"
    environment:
      JENDOCK_PUBLIC_ORIGIN: "{PUBLIC_ORIGIN}"
      JENDOCK_MCP_ALLOWED_HOSTS: "127.0.0.1:{self.api_port}"
    labels:
{env_labels}
    build:
      labels:
{labels}
  worker:
    environment:
      JENDOCK_PUBLIC_ORIGIN: "{PUBLIC_ORIGIN}"
      JENDOCK_GIT_URL_TEMPLATE: "file:///repos/{{repository}}.git"
      JENDOCK_DEPLOY_SETTLE_SECONDS: "1"
      JENDOCK_DEPLOY_HEALTH_TIMEOUT: "90"
      JENDOCK_INSPECTOR_IMAGE: "{self.project}-inspector:latest"
      JENDOCK_STEP_TIMEOUT_SECONDS: "300"
    volumes:
      - "{self.repos}:/repos:ro"
      - "{self.backups}:/backups"
    labels:
{env_labels}
    build:
      labels:
{labels}
  inspector:
    build:
      labels:
{labels}
volumes:
  data:
    labels:
{env_labels}
  worker-socket:
    labels:
{env_labels}
  keys:
    labels:
{env_labels}
networks:
  default:
    labels:
{env_labels}
"""
        )

    def environment(self) -> dict[str, str]:
        """Variables of ``compose.yml`` (host paths and the Docker group)."""
        return {
            "COMPOSE_PROJECTS_DIR": str(self.apps_dir),
            "WORKSPACES_HOST_DIR": str(self.workspaces),
            "DOCKER_GID": str(SOCKET.stat().st_gid),
            "DOCKER_SOCKET_PATH": str(SOCKET),
        }

    def compose(
        self,
        *args: str,
        input_text: str | None = None,
        timeout: float = COMMAND_TIMEOUT,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        """``docker compose`` of the platform stack."""
        argv = [
            "docker",
            "compose",
            "-p",
            self.project,
            "--project-directory",
            str(V2_ROOT),
            "-f",
            str(V2_ROOT / "compose.yml"),
            "-f",
            str(self.override_file),
            *args,
        ]
        return run(
            argv, input_text=input_text, timeout=timeout, env=self.environment(), check=check
        )

    def docker(self, *args: str, **kwargs: object) -> subprocess.CompletedProcess[str]:
        """Plain docker, for verification and one-off helpers."""
        return run(["docker", *args], **kwargs)  # type: ignore[arg-type]

    def exec_api(
        self, *args: str, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run a command in the running api container."""
        return self.compose("exec", "-T", "api", *args, input_text=input_text)

    def exec_worker(
        self, *args: str, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run a command in the running worker container."""
        return self.compose("exec", "-T", "worker", *args, input_text=input_text)

    def helper(self, image: str, script: str, *mounts: str, user: str | None = None) -> str:
        """A labelled one-off container (removed afterwards) that runs ``script`` with sh."""
        labels = [
            a for key, value in session_labels().items() for a in ("--label", f"{key}={value}")
        ]
        user_args = ["--user", user] if user else []
        volume_args = [a for mount in mounts for a in ("-v", mount)]
        result = run(
            [
                "docker",
                "run",
                "--rm",
                *labels,
                *user_args,
                *volume_args,
                "--entrypoint",
                "sh",
                image,
                "-c",
                script,
            ]
        )
        return result.stdout

    def timed(self, name: str, action: Callable[[], object]) -> None:
        """Run ``action`` and record how long it took."""
        started = time.monotonic()
        action()
        self.timings[name] = round(time.monotonic() - started, 1)

    # --- lifecycle ----------------------------------------------------------------

    def start(self) -> None:
        """Build the images from the tree, migrate, start, create the admin, wait for health."""
        for directory in (self.apps_dir, self.repos, self.backups, self.workspaces):
            directory.mkdir(parents=True, exist_ok=True)
        self.write_override()
        self.timed("build images", lambda: self.compose("build", timeout=BUILD_TIMEOUT))
        self.timed(
            "build inspector",
            lambda: self.compose("--profile", "tools", "build", "inspector", timeout=BUILD_TIMEOUT),
        )
        # The host daemon mounts these: they belong to the worker's uid and are closed to others.
        self.helper(
            "alpine:3.24",
            f"chown {UID}:{UID} /w /b && chmod 0750 /w && chmod 0777 /b",
            f"{self.workspaces}:/w",
            f"{self.backups}:/b",
        )
        self.compose("up", "--no-start")
        self.timed(
            "migrate",
            lambda: self.compose(
                "run", "--rm", "api", "python", "-m", "backend.repositories.migrate"
            ),
        )
        self.timed("up", lambda: self.compose("up", "-d"))
        self.wait_healthy()
        self.compose(
            "exec",
            "-T",
            "api",
            "python",
            "-m",
            "backend.cli",
            "users",
            "create",
            ADMIN_USER,
            "--password-stdin",
            input_text=self.admin_password + "\n",
        )
        self.exec_worker(
            "sh",
            "-c",
            "umask 077; cat > /run/jendock-keys/github_webhook_secret",
            input_text=self.webhook_secret,
        )

    def wait_healthy(self, timeout: float = 120) -> None:
        """Wait for the API health endpoint and the worker's startup log line."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(f"{self.base_url}/healthz", timeout=2) as response:  # noqa: S310
                    if (
                        response.status == 200
                        and "worker started" in self.compose("logs", "worker").stdout
                    ):
                        return
            except (urllib.error.URLError, OSError):
                pass
            time.sleep(1)
        raise CommandError(
            "the stack did not become healthy: " + self.compose("logs", "--tail", "40").stdout
        )

    def container_id(self, service: str, project: str | None = None) -> str:
        """The container id of a compose service of ``project`` (default: the platform)."""
        argv = ["docker", "compose", "-p", project or self.project, "ps", "-aq", service]
        return run(argv, env=self.environment()).stdout.strip()

    def logs(self, service: str, tail: int = 60) -> str:
        """Recent log lines of a platform service (for failure reports)."""
        return self.compose("logs", "--tail", str(tail), service, check=False).stdout

    def stop(self) -> None:
        """Remove everything this session created, in an order that leaves no network in use."""
        self.compose("down", "--volumes", "--remove-orphans", "--timeout", "2", check=False)
        run(
            ["docker", "compose", "-p", self.app_project, "down", "--volumes", "--timeout", "2"],
            check=False,
        )
        remove_test_resources()
        # Files the containers created belong to their uid: hand them back before pytest cleans up.
        if self.root.exists():
            self.helper(
                "alpine:3.24", f"chown -R {os.getuid()}:{os.getgid()} /x", f"{self.root}:/x"
            )


def dump(data: object) -> str:
    """Compact JSON for assertion messages."""
    return json.dumps(data, default=str)[:1500]
