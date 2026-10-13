"""Framing of the helper's answer on stdout.

The helper container's stdout is read line by line (docker output may be cut at 64 KiB). The
answer is JSON, compressed and base64 encoded, split into short lines tagged ``SCI1 `` and
closed by ``SCI1-END``. Any other output line (a driver warning, a traceback) is ignored by the
decoder and never shown to users.
"""

from __future__ import annotations

import base64
import binascii
import json
import zlib
from collections.abc import Iterable

FRAME_PREFIX = "SCI1 "
FRAME_END = "SCI1-END"
CHUNK_CHARS = 8000
MAX_ENCODED_CHARS = 2 * 1024 * 1024
MAX_DECODED_BYTES = 4 * 1024 * 1024


def encode(payload: dict[str, object]) -> list[str]:
    """Frame ``payload`` as output lines."""
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    text = base64.b64encode(zlib.compress(raw, 6)).decode("ascii")
    lines = [FRAME_PREFIX + text[i : i + CHUNK_CHARS] for i in range(0, len(text), CHUNK_CHARS)]
    return [*lines, FRAME_END]


def decode(lines: Iterable[str]) -> dict[str, object] | None:
    """Rebuild the payload from output lines; ``None`` when there is no complete, valid frame."""
    chunks: list[str] = []
    complete = False
    size = 0
    for line in lines:
        if line == FRAME_END:
            complete = True
        elif line.startswith(FRAME_PREFIX):
            chunk = line[len(FRAME_PREFIX) :]
            size += len(chunk)
            if size > MAX_ENCODED_CHARS:
                return None
            chunks.append(chunk)
    if not complete or not chunks:
        return None
    try:
        raw = zlib.decompressobj().decompress(
            base64.b64decode("".join(chunks), validate=True), MAX_DECODED_BYTES
        )
        decoded = json.loads(raw)
    except (binascii.Error, zlib.error, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None
