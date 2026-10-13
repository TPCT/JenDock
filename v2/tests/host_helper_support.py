"""Builders for host helper tests: a config in a temp tree and a scriptable apachectl."""

from __future__ import annotations

import os
from pathlib import Path

from backend.hosthelper.apache import ApacheControl, CommandResult
from backend.hosthelper.config import HelperConfig, PortRange
from backend.hosthelper.service import HelperService


class ScriptedApache(ApacheControl):
    """An ``ApacheControl`` that never runs a process; results are set by the test."""

    def __init__(self) -> None:
        super().__init__(("/usr/sbin/apachectl",), 5.0)
        self.configtest_ok = True
        self.reload_ok = True
        self.events: list[str] = []
        self.seen_at_configtest: list[dict[str, str]] = []
        self.watch: Path | None = None

    def configtest(self) -> CommandResult:
        """Record what the managed directory looked like when the config was tested."""
        self.events.append("configtest")
        if self.watch is not None:
            self.seen_at_configtest.append(
                {p.name: p.read_text() for p in self.watch.glob("sc-*.conf")}
            )
        return CommandResult(
            self.configtest_ok, "" if self.configtest_ok else "AH00526: Syntax error"
        )

    def reload(self) -> CommandResult:
        """Record the reload."""
        self.events.append("reload")
        return CommandResult(self.reload_ok, "" if self.reload_ok else "AH00016: not running")


def make_config(
    root: Path,
    *,
    socket_path: Path | None = None,
    allowed_uids: frozenset[int] | None = None,
    allow_wildcards: bool = False,
    max_vhosts: int = 200,
) -> HelperConfig:
    """A config whose directories live under ``root`` (created)."""
    config = HelperConfig(
        socket_path=socket_path or root / "helper.sock",
        socket_mode=0o660,
        socket_group=None,
        allowed_uids=allowed_uids or frozenset({os.geteuid()}),
        vhost_dir=root / "vhosts",
        cert_dir=root / "certs",
        webroot_dir=root / "webroot",
        apachectl=("/usr/sbin/apachectl",),
        allowed_hostname_suffixes=("example.com", "example.org"),
        allowed_upstream_ports=(PortRange(20000, 29999),),
        allow_wildcards=allow_wildcards,
        max_vhosts=max_vhosts,
    )
    config.vhost_dir.mkdir(parents=True, exist_ok=True)
    return config


def make_service(
    root: Path, **options: object
) -> tuple[HelperService, ScriptedApache, HelperConfig]:
    """A helper service with a scripted apachectl."""
    config = make_config(root, **options)  # type: ignore[arg-type]
    apache = ScriptedApache()
    apache.watch = config.vhost_dir
    return HelperService(config, apache), apache, config


def vhost_params(
    hostname: str = "app.example.com", port: int = 20001, **extra: object
) -> dict[str, object]:
    """Parameters of a ``vhost.put`` call."""
    return {"hostname": hostname, "upstream_port": port, "tls": False, "websocket": True, **extra}
