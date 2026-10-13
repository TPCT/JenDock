"""Read-only discovery of the Apache sites that already exist on the host.

The helper reads ``*.conf`` files in directories the administrator listed in its root-owned
configuration (``vhost_dir`` plus ``discovery_dirs``), never a path from a request, and returns
only what the platform needs to link a host name to a project: the names a site answers to, the
local upstream it proxies to, whether it serves TLS and the file's name. Certificate paths,
other directives and file contents are never returned. Nothing is written or executed.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from backend.hosthelper.names import is_valid_ascii_hostname
from backend.hosthelper.vhost import MARKER_PREFIX

MAX_FILES = 500
MAX_FILE_BYTES = 256 * 1024
MAX_BLOCKS = 1000
MAX_NAMES = 100
TLS_PORT = 443
_BLOCK_OPEN = re.compile(r"<virtualhost\s+(.*)>", re.IGNORECASE)
_BLOCK_CLOSE = re.compile(r"</virtualhost\s*>", re.IGNORECASE)
_HOST_PORT = re.compile(r":(\d{1,5})$")
_UPSTREAM = re.compile(
    r"(?P<scheme>https?|wss?)://(?P<host>\[[0-9a-fA-F:]+\]|[^/:\s]+)(?::(?P<port>\d{1,5}))?",
    re.IGNORECASE,
)
_PROXY_FLAG = re.compile(r"\[[^\]]*\bP\b[^\]]*\]|\[P\]", re.IGNORECASE)


@dataclass(slots=True)
class VhostBlock:
    """One ``<VirtualHost>`` as far as discovery cares."""

    file: str
    managed: bool
    ports: list[int] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    upstream_host: str | None = None
    upstream_port: int | None = None
    ssl: bool = False
    _root_proxy: bool = False

    def as_payload(self) -> dict[str, object]:
        """The wire form returned by ``vhost.discover``."""
        return {
            "file": self.file,
            "managed": self.managed,
            "server_names": self.names[:MAX_NAMES],
            "ports": self.ports,
            "upstream_host": self.upstream_host,
            "upstream_port": self.upstream_port,
            "ssl": self.ssl or TLS_PORT in self.ports,
        }


@dataclass(frozen=True, slots=True)
class ScanResult:
    """What a scan found; ``truncated`` is true when a limit cut it short."""

    blocks: tuple[VhostBlock, ...]
    files_scanned: int
    truncated: bool


def _tokens(line: str) -> list[str]:
    try:
        return shlex.split(line, comments=False, posix=True)
    except ValueError:
        return line.split()


def _logical_lines(text: str) -> list[str]:
    lines: list[str] = []
    pending = ""
    for raw in text.splitlines():
        stripped = raw.strip()
        if pending == "" and stripped.startswith("#"):
            continue
        if stripped.endswith("\\"):
            pending += stripped[:-1] + " "
            continue
        lines.append((pending + stripped).strip())
        pending = ""
    if pending:
        lines.append(pending.strip())
    return [line for line in lines if line]


def _port_of(address: str) -> int | None:
    match = _HOST_PORT.search(address)
    if match is None:
        return None
    port = int(match.group(1))
    return port if 1 <= port <= 65535 else None


def _name(value: str) -> str | None:
    host = value.strip().lower().rstrip(".")
    host = _HOST_PORT.sub("", host) if host.count(":") == 1 else host
    return host if is_valid_ascii_hostname(host, allow_wildcard=True) else None


def _upstream(url: str) -> tuple[str, int] | None:
    match = _UPSTREAM.match(url)
    if match is None:
        return None
    scheme, host, port = match.group("scheme").lower(), match.group("host"), match.group("port")
    number = int(port) if port else (443 if scheme in {"https", "wss"} else 80)
    return (host.lower(), number) if 1 <= number <= 65535 else None


def _apply(block: VhostBlock, tokens: list[str]) -> None:
    directive, args = tokens[0].lower(), tokens[1:]
    if directive == "servername" and args:
        name = _name(args[0])
        if name and name not in block.names:
            block.names.insert(0, name)
    elif directive == "serveralias":
        for alias in args:
            name = _name(alias)
            if name and name not in block.names:
                block.names.append(name)
    elif directive == "sslengine" and args:
        block.ssl = block.ssl or args[0].lower() == "on"
    elif directive in {"proxypass", "proxypassmatch"}:
        _apply_proxy(block, args)
    elif directive == "rewriterule" and len(args) >= 3 and _PROXY_FLAG.search(args[-1]):
        _apply_upstream(block, args[1], root=False)


def _apply_proxy(block: VhostBlock, args: list[str]) -> None:
    if len(args) < 2 or args[1] == "!":
        return
    _apply_upstream(block, args[1], root=args[0] == "/")


def _apply_upstream(block: VhostBlock, url: str, *, root: bool) -> None:
    found = _upstream(url)
    if found is None or (block.upstream_port is not None and (block._root_proxy or not root)):
        return
    block.upstream_host, block.upstream_port = found
    block._root_proxy = root


def parse_vhosts(text: str, file: str, *, managed: bool) -> list[VhostBlock]:
    """The ``<VirtualHost>`` blocks of one file; text outside blocks is ignored."""
    blocks: list[VhostBlock] = []
    current: VhostBlock | None = None
    for line in _logical_lines(text):
        opened = _BLOCK_OPEN.fullmatch(line)
        if opened is not None:
            current = VhostBlock(file, managed)
            for address in _tokens(opened.group(1)):
                port = _port_of(address)
                if port is not None:
                    current.ports.append(port)
            continue
        if _BLOCK_CLOSE.fullmatch(line):
            if current is not None and current.names:
                blocks.append(current)
            current = None
            continue
        if current is not None:
            tokens = _tokens(line)
            if tokens:
                _apply(current, tokens)
    return blocks


def _read(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def scan_directories(directories: Sequence[Path]) -> ScanResult:
    """Parse every ``*.conf`` file of the (administrator-configured) directories."""
    blocks: list[VhostBlock] = []
    scanned, truncated = 0, False
    seen: set[Path] = set()
    for directory in directories:
        try:
            files = sorted(directory.glob("*.conf"))
        except OSError:
            continue
        for path in files:
            try:
                real = path.resolve()
            except OSError:
                continue
            if real in seen:
                continue
            seen.add(real)
            if scanned >= MAX_FILES:
                truncated = True
                break
            text = _read(path)
            if text is None:
                continue
            scanned += 1
            managed = text.startswith(MARKER_PREFIX)
            blocks.extend(parse_vhosts(text, path.name, managed=managed))
    if len(blocks) > MAX_BLOCKS:
        blocks, truncated = blocks[:MAX_BLOCKS], True
    return ScanResult(tuple(blocks), scanned, truncated)
