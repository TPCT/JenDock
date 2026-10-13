"""``scripts/preflight.py`` against fake host layouts (it must never change anything)."""

from __future__ import annotations

import importlib.util
import os
import socket
import sys
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from types import ModuleType

import pytest

OTHER_KEYS = "DOCKER_SOCKET_PATH=/s\nDOCKER_GID=1\nTRUSTED_PROXIES=1.1.1.1\n"
SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "preflight.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("preflight_script", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["preflight_script"] = module
    spec.loader.exec_module(module)
    return module


pf = _load()


@pytest.fixture
def host(tmp_path: Path, socket_dir: Path) -> Iterator[dict[str, Path]]:
    """A complete fake host; the Docker socket is a real listening Unix socket."""
    paths = {
        "docker": tmp_path / "bin" / "docker",
        "compose": tmp_path / "bin" / "docker-compose",
        "projects": tmp_path / "projects",
        "workspaces": tmp_path / "workspaces",
        "backups": tmp_path / "backups",
        "apache": tmp_path / "apache2",
        "env": tmp_path / "env",
        "socket": socket_dir / "docker.sock",
    }
    for name in ("docker", "compose"):
        paths[name].parent.mkdir(exist_ok=True)
        paths[name].write_text("#!/bin/sh\n")
        paths[name].chmod(0o755)
    paths["projects"].mkdir()
    for name in ("workspaces", "backups"):
        paths[name].mkdir(mode=0o750)
        paths[name].chmod(0o750)
    (paths["apache"] / "mods-enabled").mkdir(parents=True)
    for module in pf.REQUIRED_APACHE_MODULES:
        (paths["apache"] / "mods-enabled" / f"{module}.load").write_text("")
    (paths["apache"] / "apache2.conf").write_text("")
    paths["env"].write_text(
        "PUBLIC_ORIGIN=https://deploy.example.test\n"
        "DOCKER_SOCKET_PATH=/run/docker-isolation.sock\nDOCKER_GID=998\nTRUSTED_PROXIES=172.18.0.1\n"
    )
    paths["env"].chmod(0o640)
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(paths["socket"]))
    server.listen()
    yield paths
    server.close()


def _targets(host: dict[str, Path], **override: object) -> object:
    values: dict[str, object] = {
        "docker_binary": host["docker"],
        "compose_binary": host["compose"],
        "docker_socket": host["socket"],
        "projects_dir": host["projects"],
        "workspaces_dir": host["workspaces"],
        "backup_dir": host["backups"],
        "apache_root": host["apache"],
        "env_file": host["env"],
        "hostname": "deploy.example.test",
        "api_port": _free_port(),
        "uid": os.getuid(),
        "gid": os.getgid(),
        "execute": False,
        "resolve_dns": False,
    }
    values.update(override)
    return pf.Targets(**values)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _by_name(results: Sequence[object]) -> dict[str, object]:
    return {r.name: r for r in results}  # type: ignore[attr-defined]


def _snapshot(root: Path) -> list[tuple[str, int, float]]:
    return sorted(
        (str(p), p.stat().st_mode, p.stat().st_mtime) for p in root.rglob("*") if p.exists()
    )


def test_a_complete_host_is_ready(host: dict[str, Path]) -> None:
    results = pf.run_checks(_targets(host), run=lambda *_: (0, "ok"))
    failed = [r for r in results if r.status is pf.Status.FAIL]
    assert failed == []
    report = pf.render(results)
    assert report.rstrip().splitlines()[-1].startswith("READY")
    assert "docker socket" in report


def test_preflight_changes_nothing(host: dict[str, Path], tmp_path: Path) -> None:
    before = _snapshot(tmp_path)
    pf.run_checks(_targets(host), run=lambda *_: (0, "ok"))
    assert _snapshot(tmp_path) == before


def test_missing_prerequisites_fail_with_a_fix(tmp_path: Path) -> None:
    targets = pf.Targets(
        docker_binary=tmp_path / "no-docker",
        compose_binary=tmp_path / "no-compose",
        docker_socket=tmp_path / "no.sock",
        projects_dir=tmp_path / "no-projects",
        workspaces_dir=tmp_path / "no-ws",
        backup_dir=tmp_path / "no-backups",
        apache_root=tmp_path / "no-apache",
        env_file=tmp_path / "no-env",
        api_port=_free_port(),
        execute=False,
        resolve_dns=False,
    )
    results = pf.run_checks(targets)
    by_name = _by_name(results)
    for name in (
        "docker CLI",
        "docker compose binary",
        "docker socket",
        "compose projects dir",
        "workspaces dir",
        "backup dir",
        "apache",
        "compose .env",
    ):
        assert by_name[name].status is pf.Status.FAIL, name  # type: ignore[attr-defined]
    assert "install -d -o 10001 -g 10001 -m 0750" in by_name["workspaces dir"].fix  # type: ignore[attr-defined]
    report = pf.render(results)
    assert "NOT READY" in report
    assert "fix:" in report


def test_wrong_ownership_and_open_mode_are_failures(host: dict[str, Path]) -> None:
    host["workspaces"].chmod(0o755)
    results = pf.run_checks(_targets(host, uid=os.getuid() + 1), run=lambda *_: (0, ""))
    detail = _by_name(results)["workspaces dir"]
    assert detail.status is pf.Status.FAIL  # type: ignore[attr-defined]
    assert "expected" in detail.detail  # type: ignore[attr-defined]
    assert "open to others" in detail.detail  # type: ignore[attr-defined]


def test_a_listening_port_fails(host: dict[str, Path]) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = int(listener.getsockname()[1])
        results = pf.run_checks(_targets(host, api_port=port))
    assert _by_name(results)[f"port 127.0.0.1:{port}"].status is pf.Status.FAIL  # type: ignore[attr-defined]


def test_missing_apache_modules_are_listed(host: dict[str, Path]) -> None:
    (host["apache"] / "mods-enabled" / "proxy_http.load").unlink()
    (host["apache"] / "mods-enabled" / "ssl.load").unlink()
    result = _by_name(pf.run_checks(_targets(host)))["apache modules"]
    assert result.status is pf.Status.FAIL  # type: ignore[attr-defined]
    assert "proxy_http" in result.detail  # type: ignore[attr-defined]
    assert result.fix == "a2enmod proxy_http ssl"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("content", "name"),
    [
        (
            "PUBLIC_ORIGIN=http://deploy.example.test\n" + OTHER_KEYS,
            "PUBLIC_ORIGIN",
        ),
        ("PUBLIC_ORIGIN=https://x.test\n", "compose .env"),
    ],
)
def test_env_file_problems(host: dict[str, Path], content: str, name: str) -> None:
    host["env"].write_text(content)
    results = pf.run_checks(_targets(host))
    assert any(r.name == name and r.status is pf.Status.FAIL for r in results)


def test_world_readable_env_file_warns(host: dict[str, Path]) -> None:
    host["env"].chmod(0o644)
    results = pf.run_checks(_targets(host))
    assert any(r.status is pf.Status.WARN and "mode" in r.name for r in results)


def test_daemon_checks_run_the_configured_binaries_with_the_socket(host: dict[str, Path]) -> None:
    calls: list[tuple[list[str], Mapping[str, str]]] = []

    def fake(argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, str]:
        calls.append((list(argv), env))
        return (0, "29.8.2") if "version" in argv else (1, "")

    results = pf.run_checks(_targets(host, execute=True), run=fake)
    assert calls[0][0][0] == str(host["docker"])
    assert calls[0][1]["DOCKER_HOST"] == f"unix://{host['socket']}"
    by_name = _by_name(results)
    assert by_name["docker daemon"].status is pf.Status.PASS  # type: ignore[attr-defined]
    assert by_name["docker compose"].status is pf.Status.PASS  # type: ignore[attr-defined]
    failing = _by_name(pf.run_checks(_targets(host, execute=True), run=lambda *_: (1, "boom")))
    assert failing["docker daemon"].status is pf.Status.FAIL  # type: ignore[attr-defined]


def test_dns_failure_is_a_warning(host: dict[str, Path]) -> None:
    result = pf.check_dns("nonexistent.invalid")
    assert result.status is pf.Status.WARN


def test_main_prints_a_report_and_sets_the_exit_code(
    host: dict[str, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    argv = [
        "--no-exec",
        "--no-dns",
        "--env-file",
        str(host["env"]),
        "--docker-binary",
        str(host["docker"]),
        "--compose-binary",
        str(host["compose"]),
        "--docker-socket",
        str(host["socket"]),
        "--projects-dir",
        str(host["projects"]),
        "--workspaces-dir",
        str(host["workspaces"]),
        "--backup-dir",
        str(host["backups"]),
        "--apache-root",
        str(host["apache"]),
        "--uid",
        str(os.getuid()),
        "--gid",
        str(os.getgid()),
        "--api-port",
        str(_free_port()),
    ]
    assert pf.main(argv, env={}) == 0
    assert "READY" in capsys.readouterr().out
    host["projects"].rmdir()
    assert pf.main(argv, env={}) == 1
    assert "NOT READY" in capsys.readouterr().out


def test_defaults_come_from_the_compose_variables() -> None:
    parser = pf.build_parser(
        {"DOCKER_SOCKET_PATH": "/x.sock", "COMPOSE_PROJECTS_DIR": "/p", "BACKUP_HOST_DIR": "/b"}
    )
    args = parser.parse_args([])
    assert (str(args.docker_socket), str(args.projects_dir), str(args.backup_dir)) == (
        "/x.sock",
        "/p",
        "/b",
    )
    assert str(pf.build_parser({}).parse_args([]).docker_socket) == "/run/docker-isolation.sock"
