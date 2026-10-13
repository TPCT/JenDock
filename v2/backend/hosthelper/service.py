"""The allowlisted operations of the host helper.

Every operation validates its parameters itself. The operation set is closed: ping, rendering
and reading managed vhost files, writing and removing them (with configtest, graceful reload
and automatic rollback), installing certificates, ACME HTTP-01 challenge files and a loopback
port probe. There is no generic command, path or configuration-text parameter.
"""

from __future__ import annotations

import contextlib
import socket
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from backend.hosthelper.apache import ApacheControl
from backend.hosthelper.certs import describe_certificate, validate_material
from backend.hosthelper.config import HelperConfig
from backend.hosthelper.errors import HelperError, InvalidRequestError
from backend.hosthelper.files import PRIVATE_DIR_MODE, ChangeSet
from backend.hosthelper.names import (
    file_stem,
    hostname_matches_suffixes,
    require_acme_content,
    require_acme_token,
    require_hostname,
    require_port,
)
from backend.hosthelper.vhost import MARKER_PREFIX, VhostPaths, VhostSpec, render_vhost

PROTOCOL_VERSION = 1
VHOST_PREFIX = "sc-"
VHOST_SUFFIX = ".conf"
VHOST_MODE = 0o644
CERT_MODE = 0o644
KEY_MODE = 0o600
CHALLENGE_MODE = 0o644
PROBE_TIMEOUT_SECONDS = 2.0
Result = dict[str, object]
Operation = Callable[[Mapping[str, object]], Result]


class HelperService:
    """Implements the operations; one instance serves all requests, one at a time."""

    def __init__(self, config: HelperConfig, apache: ApacheControl) -> None:
        self._config = config
        self._apache = apache
        self._paths = VhostPaths(config.cert_dir, config.webroot_dir)
        self._operations: dict[str, Operation] = {
            "ping": self._ping,
            "vhost.render": self._vhost_render,
            "vhost.get": self._vhost_get,
            "vhost.list": self._vhost_list,
            "vhost.put": self._vhost_put,
            "vhost.remove": self._vhost_remove,
            "cert.install": self._cert_install,
            "cert.info": self._cert_info,
            "cert.remove": self._cert_remove,
            "challenge.put": self._challenge_put,
            "challenge.remove": self._challenge_remove,
            "backend.check": self._backend_check,
            "apache.configtest": self._configtest,
        }

    def execute(self, operation: object, params: object) -> Result:
        """Run one operation; unknown names and malformed params raise ``InvalidRequestError``."""
        if not isinstance(operation, str) or operation not in self._operations:
            raise InvalidRequestError("unknown operation")
        if not isinstance(params, dict):
            raise InvalidRequestError("params must be an object")
        return self._operations[operation](params)

    # --- parameters --------------------------------------------------------------------

    def _hostname(self, params: Mapping[str, object]) -> str:
        hostname = require_hostname(
            params.get("hostname"), allow_wildcard=self._config.allow_wildcards
        )
        if not hostname_matches_suffixes(hostname, self._config.allowed_hostname_suffixes):
            raise InvalidRequestError("hostname is outside the allowed zones")
        return hostname

    def _spec(self, params: Mapping[str, object]) -> VhostSpec:
        _only(params, {"hostname", "upstream_port", "tls", "websocket"})
        port = require_port(params.get("upstream_port"))
        if not self._config.port_allowed(port):
            raise InvalidRequestError("upstream port is not allowed")
        tls = _boolean(params.get("tls"), "tls")
        websocket = _boolean(params.get("websocket", True), "websocket")
        return VhostSpec(self._hostname(params), port, tls, websocket)

    def _vhost_path(self, hostname: str) -> Path:
        path = self._config.vhost_dir / f"{VHOST_PREFIX}{file_stem(hostname)}{VHOST_SUFFIX}"
        if path.parent != self._config.vhost_dir:  # defence in depth; names are validated
            raise InvalidRequestError("invalid vhost path")
        return path

    def _cert_paths(self, hostname: str) -> tuple[Path, Path, Path]:
        directory = self._config.cert_dir / file_stem(hostname)
        return directory, directory / "fullchain.pem", directory / "privkey.pem"

    # --- operations --------------------------------------------------------------------

    def _ping(self, params: Mapping[str, object]) -> Result:
        _only(params, set())
        return {"version": PROTOCOL_VERSION}

    def _configtest(self, params: Mapping[str, object]) -> Result:
        _only(params, set())
        result = self._apache.configtest()
        return {"ok": result.ok, "output": result.output}

    def _vhost_render(self, params: Mapping[str, object]) -> Result:
        spec = self._spec(params)
        current = self._read_managed(spec.hostname)
        return {"proposed": render_vhost(spec, self._paths), "current": current}

    def _vhost_get(self, params: Mapping[str, object]) -> Result:
        _only(params, {"hostname"})
        current = self._read_managed(self._hostname(params))
        return {"exists": current is not None, "content": current}

    def _vhost_list(self, params: Mapping[str, object]) -> Result:
        _only(params, set())
        names = []
        for path in sorted(self._config.vhost_dir.glob(f"{VHOST_PREFIX}*{VHOST_SUFFIX}")):
            first = _first_line(path)
            if first.startswith(MARKER_PREFIX):
                names.append(first.split("host=", 1)[1].split(" ", 1)[0])
        return {"hostnames": names}

    def _vhost_put(self, params: Mapping[str, object]) -> Result:
        spec = self._spec(params)
        if spec.tls and not all(p.is_file() for p in self._cert_paths(spec.hostname)[1:]):
            raise HelperError("certificate_missing", "no certificate is installed for the hostname")
        path = self._vhost_path(spec.hostname)
        content = render_vhost(spec, self._paths)
        existing = self._read_managed(spec.hostname)
        if existing == content:
            return {"changed": False}
        if existing is None and len(self._vhost_list({})["hostnames"]) >= self._config.max_vhosts:  # type: ignore[arg-type]
            raise HelperError("limit_reached", "too many managed vhosts")
        changes = ChangeSet()
        changes.write(path, content.encode(), VHOST_MODE)
        self._test_and_reload(changes)
        return {"changed": True}

    def _vhost_remove(self, params: Mapping[str, object]) -> Result:
        _only(params, {"hostname"})
        hostname = self._hostname(params)
        if self._read_managed(hostname) is None:
            return {"removed": False}
        changes = ChangeSet()
        changes.delete(self._vhost_path(hostname))
        self._test_and_reload(changes)
        return {"removed": True}

    def _cert_install(self, params: Mapping[str, object]) -> Result:
        _only(params, {"hostname", "fullchain_pem", "private_key_pem"})
        hostname = self._hostname(params)
        chain, key = params.get("fullchain_pem"), params.get("private_key_pem")
        if not isinstance(chain, str) or not isinstance(key, str):
            raise InvalidRequestError("certificate material must be PEM text")
        info = validate_material(hostname, chain, key)
        directory, chain_path, key_path = self._cert_paths(hostname)
        directory.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
        changes = ChangeSet()
        changes.write(key_path, key.encode(), KEY_MODE)
        changes.write(chain_path, chain.encode(), CERT_MODE)
        if self._read_managed(hostname) is not None:
            self._test_and_reload(changes)
        return {"not_after": info.not_after.isoformat(), "serial": info.serial}

    def _cert_info(self, params: Mapping[str, object]) -> Result:
        _only(params, {"hostname"})
        _, chain_path, _ = self._cert_paths(self._hostname(params))
        if not chain_path.is_file():
            return {"installed": False, "not_after": None}
        info = describe_certificate(chain_path.read_text(encoding="utf-8"))
        return {"installed": True, "not_after": _iso(info.not_after), "serial": info.serial}

    def _cert_remove(self, params: Mapping[str, object]) -> Result:
        _only(params, {"hostname"})
        hostname = self._hostname(params)
        current = self._read_managed(hostname)
        if current is not None and "SSLEngine on" in current:
            raise HelperError("in_use", "the vhost still uses the certificate; remove it first")
        directory, chain_path, key_path = self._cert_paths(hostname)
        existed = chain_path.exists() or key_path.exists()
        chain_path.unlink(missing_ok=True)
        key_path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            directory.rmdir()
        return {"removed": existed}

    def _challenge_put(self, params: Mapping[str, object]) -> Result:
        _only(params, {"token", "content"})
        token = require_acme_token(params.get("token"))
        content = require_acme_content(params.get("content"))
        directory = self._paths.challenge_dir()
        directory.mkdir(parents=True, exist_ok=True)
        ChangeSet().write(directory / token, content.encode(), CHALLENGE_MODE)
        return {"written": True}

    def _challenge_remove(self, params: Mapping[str, object]) -> Result:
        _only(params, {"token"})
        token = require_acme_token(params.get("token"))
        path = self._paths.challenge_dir() / token
        existed = path.exists()
        path.unlink(missing_ok=True)
        return {"removed": existed}

    def _backend_check(self, params: Mapping[str, object]) -> Result:
        _only(params, {"port"})
        port = require_port(params.get("port"))
        if not self._config.port_allowed(port):
            raise InvalidRequestError("upstream port is not allowed")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=PROBE_TIMEOUT_SECONDS):
                return {"reachable": True}
        except OSError:
            return {"reachable": False}

    # --- shared steps ------------------------------------------------------------------

    def _read_managed(self, hostname: str) -> str | None:
        """Return the managed vhost text; refuse to treat a foreign file as ours."""
        path = self._vhost_path(hostname)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        if not text.startswith(MARKER_PREFIX):
            raise HelperError("foreign_file", "a file not managed by Jendock exists at that name")
        return text

    def _test_and_reload(self, changes: ChangeSet) -> None:
        """Validate the new configuration, reload, and restore the old files on any failure."""
        test = self._apache.configtest()
        if not test.ok:
            changes.rollback()
            raise HelperError(
                "configtest_failed", f"configtest failed, change reverted: {test.output}"
            )
        reload = self._apache.reload()
        if not reload.ok:
            changes.rollback()
            self._apache.reload()  # best effort: bring the running server back to the old files
            raise HelperError("reload_failed", f"reload failed, change reverted: {reload.output}")


def _only(params: Mapping[str, object], allowed: set[str]) -> None:
    extra = set(params) - allowed
    if extra:
        raise InvalidRequestError(f"unexpected parameters: {sorted(extra)}")


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise InvalidRequestError(f"{name} must be true or false")
    return value


def _first_line(path: Path) -> str:
    try:
        with path.open(encoding="utf-8") as handle:
            return handle.readline().strip()
    except OSError:
        return ""


def _iso(value: datetime) -> str:
    return value.isoformat()
