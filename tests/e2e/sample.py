"""The sample application: a git repository the worker clones and a compose project it deploys.

The repository holds a tiny web app (busybox ``httpd``) and a ``Jenkinsfile`` that builds the
image, runs a test in it, deploys the ``web`` service with an HTTP health check and checks the
result. The compose project (two app services and a seeded postgres:16, with an ``--env-file``
interpolated port and password) lives in the projects directory the worker mounts read-only.
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from tests.docker_support import session_labels
from tests.e2e.stack import PUBLIC_ORIGIN, UID, Stack, label_lines, run

REPOSITORY = "acme/app"
BASE = "busybox:stable"
WEB_URL = "http://web:8080/"
DEPLOY = "deploy(service: 'web', image: 'web', healthUrl: 'http://web:8080/', healthTimeout: 20)"
JENKINSFILE = """pipeline {
    agent none
    stages {
        stage('Build') {
            steps {
                dockerBuild(image: 'web')
            }
        }
        stage('Test') {
            agent { docker 'web' }
            steps {
                sh 'test -f /srv/www/health && echo tests-passed'
            }
        }
        stage('Deploy') {
            steps {
                {DEPLOY}
            }
        }
        stage('Verify') {
            steps {
                healthCheck(url: 'http://web:8080/health', timeout: 20)
            }
        }
    }
}
"""
JENKINSFILE = JENKINSFILE.replace("{DEPLOY}", DEPLOY)
INIT_SQL = """CREATE TABLE items (id serial PRIMARY KEY, name text NOT NULL);
INSERT INTO items (name) VALUES ('alpha'), ('beta'), ('gamma');
"""


def dockerfile(version: str) -> str:
    """The app image; carries the session labels so teardown finds it."""
    labels = " ".join(f'"{key}"="{value}"' for key, value in session_labels().items())
    return (
        f'FROM {BASE}\nLABEL {labels}\nLABEL app.version="{version}"\n'
        "COPY www /srv/www\nCOPY run.sh /run.sh\nEXPOSE 8080\n"
        'HEALTHCHECK NONE\nCMD ["sh", "/run.sh"]\n'
    )


def run_script(version: str) -> str:
    return f'#!/bin/sh\necho "web version {version} starting"\nexec httpd -f -p 8080 -h /srv/www\n'


@dataclass(slots=True)
class SampleRepo:
    """The bare repository served to the worker and the working tree it is committed from."""

    stack: Stack
    work: Path

    @property
    def bare(self) -> str:
        return f"/repos/{REPOSITORY}.git"

    def commit(self, version: str, *, healthy: bool = True) -> str:
        """Write the app at ``version`` (``healthy`` False omits ``index.html``), commit, push.

        Git runs as the worker's uid in a throwaway container, so the repository is owned by
        the user that later clones it (git refuses repositories of other users).
        """
        shutil.rmtree(self.work / "www", ignore_errors=True)  # never .git: it is the worker uid's
        (self.work / "www").mkdir(parents=True, exist_ok=True)
        (self.work / "www" / "health").write_text("ok\n")
        if healthy:
            (self.work / "www" / "index.html").write_text(f"version {version}\n")
        (self.work / "Dockerfile").write_text(dockerfile(version))
        (self.work / "run.sh").write_text(run_script(version))
        (self.work / "Jenkinsfile").write_text(JENKINSFILE)
        self.work.chmod(0o777)
        script = (
            "set -e; cd /w; "
            "[ -d .git ] || git init -q -b main .; "
            "git -c safe.directory=/w add -A; "
            "git -c safe.directory=/w -c user.name=e2e -c user.email=e2e@example.test "
            f"commit -q -m 'version {version}'; "
            f"[ -d {self.bare} ] || git init -q --bare {self.bare}; "
            f"git -c safe.directory=/w push -q {self.bare} HEAD:refs/heads/main --force; "
            "git -c safe.directory=/w rev-parse HEAD"
        )
        return self.stack.helper(
            self.stack.worker_image,
            script,
            f"{self.work}:/w",
            f"{self.stack.repos}:/repos",
            user=f"{UID}:{UID}",
        ).strip()


def create_repo(stack: Stack) -> SampleRepo:
    """The (empty) hosting setup; the first ``commit`` creates the bare repository."""
    stack.repos.chmod(0o777)
    (stack.repos / "acme").mkdir(exist_ok=True)
    (stack.repos / "acme").chmod(0o777)
    return SampleRepo(stack, stack.root / "work")


@dataclass(slots=True)
class SampleProject:
    """The deployed compose project and where its files are."""

    stack: Stack
    directory: Path

    @property
    def compose_file(self) -> Path:
        return self.directory / "compose.yml"

    @property
    def env_file(self) -> Path:
        return self.directory / "ports.env"

    def wait_healthy(self, *services: str, timeout: float = 120) -> None:
        """Wait until every named service reports ``healthy`` (they all define a healthcheck)."""
        deadline = time.monotonic() + timeout
        pending = set(services)
        while pending and time.monotonic() < deadline:
            for service in sorted(pending):
                cid = self.compose("ps", "-q", service).strip()
                state = run(
                    ["docker", "inspect", "--format", "{{.State.Health.Status}}", cid], check=False
                ).stdout.strip()
                if state == "healthy":
                    pending.discard(service)
            time.sleep(1)
        assert not pending, f"not healthy in time: {sorted(pending)}"

    def compose(self, *args: str) -> str:
        """``docker compose`` of the sample project (outside the platform)."""
        return run(
            [
                "docker",
                "compose",
                "-p",
                self.stack.app_project,
                "--env-file",
                str(self.env_file),
                "-f",
                str(self.compose_file),
                *args,
            ],
            timeout=300,
        ).stdout


def create_project(stack: Stack, seed_image: str) -> SampleProject:
    """Write the compose project, start it once (version zero) and attach the worker's network."""
    directory = stack.apps_dir / "sample"
    directory.mkdir(parents=True, exist_ok=True)
    labels = "{" + ", ".join(f'"{k}": "{v}"' for k, v in session_labels().items()) + "}"
    (directory / "init.sql").write_text(INIT_SQL)
    (directory / "ports.env").write_text(
        f"WEB_PORT={stack.web_port}\nDB_PASSWORD={stack.db_password}\n"
    )
    (directory / "compose.yml").write_text(
        f"""services:
  web:
    image: {seed_image}
    labels: {labels}
    ports: ["127.0.0.1:${{WEB_PORT:?set WEB_PORT}}:8080"]
    healthcheck:
      test: ["CMD", "wget", "-qO-", "http://127.0.0.1:8080/health"]
      interval: 1s
      timeout: 2s
      retries: 3
    stop_signal: SIGKILL
  worker:
    image: {seed_image}
    command: ["sleep", "3600"]
    labels: {labels}
    stop_signal: SIGKILL
  db:
    image: postgres:16
    labels: {labels}
    environment:
      POSTGRES_PASSWORD: "${{DB_PASSWORD:?set DB_PASSWORD}}"
      POSTGRES_DB: app
    volumes:
      - ./init.sql:/docker-entrypoint-initdb.d/init.sql:ro
      - dbdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres -d app"]
      interval: 1s
      timeout: 3s
      retries: 30
    stop_signal: SIGKILL
volumes:
  dbdata:
    labels:
{label_lines(6)}
networks:
  default:
    labels:
{label_lines(6)}
"""
    )
    project = SampleProject(stack, directory)
    project.compose("up", "-d")
    project.wait_healthy("web", "db")
    run(
        [
            "docker",
            "network",
            "connect",
            f"{stack.app_project}_default",
            stack.container_id("worker"),
        ]
    )
    return project


def build_seed_image(stack: Stack) -> str:
    """The version-zero image the sample project starts with (a labelled build)."""
    context = stack.root / "seed"
    (context / "www").mkdir(parents=True, exist_ok=True)
    (context / "www" / "health").write_text("ok\n")
    (context / "www" / "index.html").write_text("version seed\n")
    (context / "Dockerfile").write_text(dockerfile("seed"))
    (context / "run.sh").write_text(run_script("seed"))
    tag = f"jdtest-e2e-{stack.id}-seed:v0"
    run(["docker", "build", "--quiet", "--tag", tag, str(context)], timeout=300)
    return tag


__all__ = ["PUBLIC_ORIGIN", "REPOSITORY", "WEB_URL", "create_project", "create_repo"]
