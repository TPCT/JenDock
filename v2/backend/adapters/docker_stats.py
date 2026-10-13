"""Parsing of ``docker stats --no-stream --format '{{json .}}'`` output."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

_SIZE = re.compile(r"([0-9]*\.?[0-9]+)\s*([A-Za-z]*)")
_UNITS = {
    "": 1,
    "b": 1,
    "kb": 1000,
    "mb": 1000**2,
    "gb": 1000**3,
    "tb": 1000**4,
    "kib": 1024,
    "mib": 1024**2,
    "gib": 1024**3,
    "tib": 1024**4,
}


@dataclass(frozen=True, slots=True)
class ContainerStats:
    """One ``docker stats`` sample of a container."""

    id: str
    name: str
    cpu_percent: float
    memory_used: int
    memory_limit: int
    memory_percent: float
    net_rx: int
    net_tx: int
    block_read: int
    block_write: int
    pids: int


def parse_size(text: str) -> int:
    """``1.5MiB`` or ``2kB`` to bytes; unparsable text is 0."""
    match = _SIZE.fullmatch(text.strip())
    if match is None:
        return 0
    unit = _UNITS.get(match.group(2).lower())
    return int(float(match.group(1)) * unit) if unit is not None else 0


def parse_percent(text: str) -> float:
    """``12.5%`` to 12.5; ``--`` and garbage are 0."""
    try:
        return float(text.strip().rstrip("%"))
    except ValueError:
        return 0.0


def _pair(text: str) -> tuple[int, int]:
    left, _sep, right = text.partition("/")
    return parse_size(left), parse_size(right)


def parse_stats_line(line: str) -> ContainerStats | None:
    """One JSON line of docker stats; malformed lines are skipped."""
    try:
        raw = json.loads(line)
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    used, limit = _pair(str(raw.get("MemUsage", "")))
    rx, tx = _pair(str(raw.get("NetIO", "")))
    read, write = _pair(str(raw.get("BlockIO", "")))
    pids = str(raw.get("PIDs", "0"))
    return ContainerStats(
        id=str(raw.get("ID") or raw.get("Container") or ""),
        name=str(raw.get("Name", "")),
        cpu_percent=parse_percent(str(raw.get("CPUPerc", ""))),
        memory_used=used,
        memory_limit=limit,
        memory_percent=parse_percent(str(raw.get("MemPerc", ""))),
        net_rx=rx,
        net_tx=tx,
        block_read=read,
        block_write=write,
        pids=int(pids) if pids.isdigit() else 0,
    )


def parse_stats(output: str) -> list[ContainerStats]:
    """All samples of a docker stats output."""
    parsed = (parse_stats_line(line) for line in output.splitlines() if line.strip())
    return [item for item in parsed if item is not None]
