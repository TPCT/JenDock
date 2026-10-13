#!/usr/bin/env python3
"""Read-only preflight of a host before Jendock is started on it.

Checks every prerequisite listed in docs/deployment.md and prints a report; it changes nothing
(no file is written, no container started, no service touched). Exit status 1 when any check
FAILs, so it can gate the checklist. Standard library only: it runs with the host's python3.

    python3 scripts/preflight.py                 # defaults match the production override
    make preflight PREFLIGHT_ARGS="--hostname deploy.example.com"

Defaults come from the same environment variables as compose.yml (DOCKER_SOCKET_PATH,
COMPOSE_PROJECTS_DIR, WORKSPACES_HOST_DIR, BACKUP_HOST_DIR, ...), then from the paths of the
usual production host layout.
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import stat
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

WORKER_UID = 10001
SHARED_GID = 10001
MIN_FREE_GIB = 5
REQUIRED_APACHE_MODULES = ("proxy", "proxy_http", "headers", "ssl", "rewrite")
REQUIRED_ENV_KEYS = ("PUBLIC_ORIGIN", "DOCKER_SOCKET_PATH", "DOCKER_GID", "TRUSTED_PROXIES")
EXEC_TIMEOUT = 20


class Status(StrEnum):
    """Outcome of one check."""

    PASS = "PASS"  # noqa: S105 - a status word, not a credential
    WARN = "WARN"
    FAIL = "FAIL"
    SKIP = "SKIP"


@dataclass(frozen=True, slots=True)
class Result:
    """One line of the report; ``fix`` says what to do when it is not a PASS."""

    name: str
    status: Status
    detail: str
    fix: str = ""


@dataclass(frozen=True, slots=True)
class Targets:
    """Everything the checks look at (all overridable so tests can use fake paths)."""

    docker_binary: Path = Path("/opt/docker-engine/29.8.2/docker/docker")
    compose_binary: Path = Path("/opt/docker-engine/tools/docker-compose")
    docker_socket: Path = Path("/run/docker-isolation.sock")
    projects_dir: Path = Path("/opt/docker-engine/projects")
    workspaces_dir: Path = Path("/var/lib/jendock-workspaces")
    backup_dir: Path = Path("/var/backups/jendock")
    apache_root: Path = Path("/etc/apache2")
    env_file: Path | None = None
    hostname: str = "deploy.example.com"
    api_port: int = 8000
    uid: int = WORKER_UID
    gid: int = SHARED_GID
    execute: bool = True
    resolve_dns: bool = True
    extra_paths: Mapping[str, Path] = field(default_factory=dict)


# --- individual checks -----------------------------------------------------------------------


def check_executable(name: str, path: Path, hint: str) -> Result:
    """The file exists, is a regular file and is executable."""
    if not path.is_file():
        return Result(name, Status.FAIL, f"{path} does not exist", hint)
    if not os.access(path, os.X_OK):
        return Result(name, Status.FAIL, f"{path} is not executable", f"chmod +x {path}")
    return Result(name, Status.PASS, str(path))


def check_socket(path: Path) -> Result:
    """The Docker socket exists, is a socket and the current user may use it."""
    name = "docker socket"
    if not path.exists():
        return Result(name, Status.FAIL, f"{path} does not exist", "start the isolated daemon")
    if not stat.S_ISSOCK(path.stat().st_mode):
        return Result(name, Status.FAIL, f"{path} is not a socket")
    info = path.stat()
    detail = f"{path} (gid {info.st_gid}: set DOCKER_GID={info.st_gid})"
    if not os.access(path, os.R_OK | os.W_OK):
        return Result(
            name,
            Status.WARN,
            detail + "; the current user cannot use it",
            "run the preflight as a user in that group, or with sudo",
        )
    return Result(name, Status.PASS, detail)


def check_readable_dir(name: str, path: Path, fix: str) -> Result:
    """A directory the worker must read."""
    if not path.is_dir():
        return Result(name, Status.FAIL, f"{path} is not a directory", fix)
    if not os.access(path, os.R_OK | os.X_OK):
        return Result(name, Status.FAIL, f"{path} is not readable", fix)
    return Result(name, Status.PASS, str(path))


def check_owned_dir(name: str, path: Path, uid: int, gid: int) -> Result:
    """A private host directory owned by the worker user, closed to others."""
    fix = f"install -d -o {uid} -g {gid} -m 0750 {path}"
    if not path.is_dir():
        return Result(name, Status.FAIL, f"{path} does not exist", fix)
    info = path.stat()
    mode = stat.S_IMODE(info.st_mode)
    problems = []
    if info.st_uid != uid:
        problems.append(f"owner is uid {info.st_uid}, expected {uid}")
    if mode & 0o007:
        problems.append(f"mode {mode:04o} is open to others")
    if mode & 0o700 != 0o700:
        problems.append(f"mode {mode:04o} is not rwx for the owner")
    if problems:
        return Result(name, Status.FAIL, f"{path}: " + "; ".join(problems), fix)
    return Result(name, Status.PASS, f"{path} (uid {info.st_uid}, mode {mode:04o})")


def check_free_space(path: Path, minimum_gib: int) -> Result:
    """Enough room for builds and workspaces."""
    name = "disk space"
    if not path.exists():
        return Result(name, Status.SKIP, f"{path} does not exist yet")
    free = shutil.disk_usage(path).free / 1024**3
    if free < minimum_gib:
        return Result(
            name, Status.WARN, f"{free:.1f} GiB free at {path}", f"free {minimum_gib} GiB"
        )
    return Result(name, Status.PASS, f"{free:.1f} GiB free at {path}")


def check_port_free(port: int) -> Result:
    """Nothing listens on the loopback port the API will use (a connect attempt only)."""
    name = f"port 127.0.0.1:{port}"
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        if probe.connect_ex(("127.0.0.1", port)) == 0:
            return Result(
                name,
                Status.FAIL,
                "something already listens there",
                "pick another port (edit the vhost and the compose port) or stop it",
            )
    return Result(name, Status.PASS, "free")


def check_apache(root: Path) -> list[Result]:
    """Apache is installed with the modules the vhost needs (read from the config tree)."""
    results = []
    if not (root / "apache2.conf").exists() and not (root / "httpd.conf").exists():
        return [
            Result(
                "apache",
                Status.FAIL,
                f"no Apache configuration in {root}",
                "pass --apache-root of the host Apache that will serve Jendock",
            )
        ]
    results.append(Result("apache", Status.PASS, f"configuration in {root}"))
    enabled = root / "mods-enabled"
    if not enabled.is_dir():
        results.append(
            Result(
                "apache modules",
                Status.SKIP,
                f"{enabled} not found: check {', '.join(REQUIRED_APACHE_MODULES)} by hand",
            )
        )
        return results
    missing = [m for m in REQUIRED_APACHE_MODULES if not (enabled / f"{m}.load").exists()]
    if missing:
        results.append(
            Result(
                "apache modules",
                Status.FAIL,
                "not enabled: " + ", ".join(missing),
                "a2enmod " + " ".join(missing),
            )
        )
    else:
        results.append(Result("apache modules", Status.PASS, ", ".join(REQUIRED_APACHE_MODULES)))
    return results


def parse_env_file(path: Path) -> dict[str, str]:
    """``KEY=value`` lines of a compose ``.env`` file (comments and blanks ignored)."""
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, _, value = text.partition("=")
        values[key.strip()] = value.strip().strip("\"'")
    return values


def check_env_file(path: Path | None) -> list[Result]:
    """The compose ``.env`` has the values the production override requires."""
    name = "compose .env"
    if path is None:
        return [
            Result(
                name,
                Status.SKIP,
                "no --env-file given",
                "copy deployment/env.production.example to .env and fill it in",
            )
        ]
    if not path.is_file():
        return [
            Result(
                name,
                Status.FAIL,
                f"{path} does not exist",
                "copy deployment/env.production.example to .env and fill it in",
            )
        ]
    values = parse_env_file(path)
    results = []
    missing = [k for k in REQUIRED_ENV_KEYS if not values.get(k)]
    if missing:
        results.append(Result(name, Status.FAIL, "missing: " + ", ".join(missing)))
    else:
        results.append(Result(name, Status.PASS, str(path)))
    origin = values.get("PUBLIC_ORIGIN", "")
    if origin and not origin.startswith("https://"):
        results.append(Result("PUBLIC_ORIGIN", Status.FAIL, f"{origin!r} is not https://"))
    if path.stat().st_mode & 0o004:
        results.append(Result(name + " mode", Status.WARN, "world-readable", f"chmod 0640 {path}"))
    return results


def check_dns(hostname: str) -> Result:
    """The Jendock host name resolves (the vhost and certificate need it)."""
    name = f"dns {hostname}"
    try:
        addresses = sorted({info[4][0] for info in socket.getaddrinfo(hostname, None)})
    except OSError as error:
        return Result(
            name,
            Status.WARN,
            f"does not resolve ({error})",
            "create the DNS record before requesting the certificate",
        )
    return Result(name, Status.PASS, ", ".join(addresses))


def run_command(argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, str]:
    """Run a short read-only command; returns (exit code, first line of output)."""
    try:
        done = subprocess.run(  # noqa: S603 - fixed argv, never a shell
            list(argv),
            capture_output=True,
            text=True,
            timeout=EXEC_TIMEOUT,
            env=dict(env),
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return 127, str(error)
    text = (done.stdout or done.stderr).strip()
    return done.returncode, text.splitlines()[0] if text else ""


def check_daemon(
    targets: Targets, run: Callable[[Sequence[str], Mapping[str, str]], tuple[int, str]]
) -> list[Result]:
    """The docker CLI reaches the isolated daemon and compose works (queries only)."""
    env = {"PATH": os.environ.get("PATH", ""), "DOCKER_HOST": f"unix://{targets.docker_socket}"}
    code, text = run(
        [str(targets.docker_binary), "version", "--format", "{{.Server.Version}}"], env
    )
    results = [
        Result(
            "docker daemon",
            Status.PASS if code == 0 else Status.FAIL,
            f"server {text}" if code == 0 else text or "no answer",
            "" if code == 0 else "check the socket path and your group",
        )
    ]
    code, text = run([str(targets.compose_binary), "version"], env)
    results.append(
        Result(
            "docker compose",
            Status.PASS if code == 0 else Status.FAIL,
            text or "no output",
            "" if code == 0 else "fix COMPOSE binary path",
        )
    )
    return results


def check_user() -> Result:
    """Who runs the preflight, for the permission checks above."""
    return Result("current user", Status.PASS, f"uid {os.getuid()}, gid {os.getgid()}")


# --- assembly --------------------------------------------------------------------------------


def run_checks(
    targets: Targets,
    run: Callable[[Sequence[str], Mapping[str, str]], tuple[int, str]] = run_command,
) -> list[Result]:
    """Every check, in the order of the checklist."""
    results = [check_user()]
    results.append(
        check_executable(
            "docker CLI",
            targets.docker_binary,
            "set DOCKER_BINARY_HOST_PATH to the host's docker binary",
        )
    )
    results.append(
        check_executable(
            "docker compose binary",
            targets.compose_binary,
            "set DOCKER_COMPOSE_HOST_PATH to the host's compose binary",
        )
    )
    results.append(check_socket(targets.docker_socket))
    results.append(
        check_readable_dir(
            "compose projects dir",
            targets.projects_dir,
            "set COMPOSE_PROJECTS_DIR to the host projects directory",
        )
    )
    results.append(
        check_owned_dir("workspaces dir", targets.workspaces_dir, targets.uid, targets.gid)
    )
    results.append(check_owned_dir("backup dir", targets.backup_dir, targets.uid, targets.gid))
    results.append(check_free_space(targets.workspaces_dir, MIN_FREE_GIB))
    results.append(check_port_free(targets.api_port))
    results.extend(check_apache(targets.apache_root))
    results.extend(check_env_file(targets.env_file))
    if targets.resolve_dns:
        results.append(check_dns(targets.hostname))
    else:
        results.append(Result(f"dns {targets.hostname}", Status.SKIP, "--no-dns"))
    if targets.execute:
        results.extend(check_daemon(targets, run))
    else:
        results.append(Result("docker daemon", Status.SKIP, "--no-exec"))
    for label, path in targets.extra_paths.items():
        results.append(check_readable_dir(label, path, "create it"))
    return results


def render(results: Sequence[Result]) -> str:
    """The report: one line per check, fixes underneath, a verdict at the end."""
    width = max(len(r.name) for r in results)
    lines = []
    for result in results:
        lines.append(f"[{result.status.value}] {result.name.ljust(width)}  {result.detail}")
        if result.fix and result.status in {Status.FAIL, Status.WARN}:
            lines.append(f"       {'':{width}}  fix: {result.fix}")
    failed = sum(r.status is Status.FAIL for r in results)
    warned = sum(r.status is Status.WARN for r in results)
    verdict = "NOT READY" if failed else ("READY (with warnings)" if warned else "READY")
    lines.append("")
    lines.append(
        f"{verdict}: {failed} failed, {warned} warnings, "
        f"{sum(r.status is Status.PASS for r in results)} passed"
    )
    return "\n".join(lines)


def build_parser(env: Mapping[str, str]) -> argparse.ArgumentParser:
    """Arguments; every default comes from the compose variable of the same meaning."""
    defaults = Targets()
    parser = argparse.ArgumentParser(description="Read-only host preflight for Jendock")
    parser.add_argument("--env-file", type=Path, help="the compose .env to validate")
    parser.add_argument("--hostname", default=env.get("DEPLOY_HOSTNAME", defaults.hostname))
    parser.add_argument("--api-port", type=int, default=defaults.api_port)
    parser.add_argument(
        "--docker-binary",
        type=Path,
        default=Path(env.get("DOCKER_BINARY_HOST_PATH", str(defaults.docker_binary))),
    )
    parser.add_argument(
        "--compose-binary",
        type=Path,
        default=Path(env.get("DOCKER_COMPOSE_HOST_PATH", str(defaults.compose_binary))),
    )
    parser.add_argument(
        "--docker-socket",
        type=Path,
        default=Path(env.get("DOCKER_SOCKET_PATH", str(defaults.docker_socket))),
    )
    parser.add_argument(
        "--projects-dir",
        type=Path,
        default=Path(env.get("COMPOSE_PROJECTS_DIR", str(defaults.projects_dir))),
    )
    parser.add_argument(
        "--workspaces-dir",
        type=Path,
        default=Path(env.get("WORKSPACES_HOST_DIR", str(defaults.workspaces_dir))),
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=Path(env.get("BACKUP_HOST_DIR", str(defaults.backup_dir))),
    )
    parser.add_argument("--apache-root", type=Path, default=defaults.apache_root)
    parser.add_argument("--uid", type=int, default=defaults.uid)
    parser.add_argument("--gid", type=int, default=defaults.gid)
    parser.add_argument("--no-exec", action="store_true", help="do not run docker queries")
    parser.add_argument("--no-dns", action="store_true", help="do not resolve the host name")
    return parser


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    """Run the checks, print the report; exit status 1 when something FAILs."""
    args = build_parser(env if env is not None else os.environ).parse_args(argv)
    targets = Targets(
        docker_binary=args.docker_binary,
        compose_binary=args.compose_binary,
        docker_socket=args.docker_socket,
        projects_dir=args.projects_dir,
        workspaces_dir=args.workspaces_dir,
        backup_dir=args.backup_dir,
        apache_root=args.apache_root,
        env_file=args.env_file,
        hostname=args.hostname,
        api_port=args.api_port,
        uid=args.uid,
        gid=args.gid,
        execute=not args.no_exec,
        resolve_dns=not args.no_dns,
    )
    results = run_checks(targets)
    print(render(results))
    return 1 if any(r.status is Status.FAIL for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
