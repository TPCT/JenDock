"""Binary frame protocol between the API (WebSocket bridge) and the worker (PTY owner).

A frame is ``type (1 byte) | payload length (4 bytes, big endian) | payload``. ``DATA`` payloads
start with a 4 byte sequence number (per direction, starting at 0); every other payload is a
JSON object. Sizes are bounded so neither side buffers unboundedly. Nothing in this module
logs or stores payloads: terminal input and output are never persisted.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from enum import IntEnum

HEADER = struct.Struct(">BI")
SEQUENCE = struct.Struct(">I")
MAX_JSON_BYTES = 4096
MAX_DATA_BYTES = 16 * 1024
MAX_FRAME_BYTES = MAX_DATA_BYTES + SEQUENCE.size
SEQUENCE_MODULUS = 2**32


class FrameType(IntEnum):
    """Frame kinds."""

    OPEN = 1  # API -> worker: what to open (JSON)
    DATA = 2  # both: terminal bytes with sequence number
    RESIZE = 3  # API -> worker: {"rows": int, "cols": int}
    CLOSE = 4  # API -> worker: end the terminal now
    READY = 5  # worker -> API: {"terminal_id": str}
    EXIT = 6  # worker -> API: {"code": int | null, "reason": str}
    ERROR = 7  # worker -> API: {"message": str}; the connection ends


class ProtocolError(Exception):
    """A frame violated the protocol (bad type, size or sequence)."""


@dataclass(frozen=True, slots=True)
class Frame:
    """A decoded frame."""

    type: FrameType
    payload: bytes


def encode_frame(frame_type: FrameType, payload: bytes = b"") -> bytes:
    """Serialize one frame."""
    return HEADER.pack(frame_type, len(payload)) + payload


def encode_json(frame_type: FrameType, body: dict[str, object]) -> bytes:
    """Serialize a JSON frame."""
    return encode_frame(frame_type, json.dumps(body, separators=(",", ":")).encode())


def encode_data(sequence: int, data: bytes) -> bytes:
    """Serialize a DATA frame."""
    if len(data) > MAX_DATA_BYTES:
        raise ProtocolError("data frame too large")
    return encode_frame(FrameType.DATA, SEQUENCE.pack(sequence % SEQUENCE_MODULUS) + data)


def decode_data(frame: Frame) -> tuple[int, bytes]:
    """Split a DATA frame into ``(sequence, bytes)``."""
    if frame.type is not FrameType.DATA or len(frame.payload) < SEQUENCE.size:
        raise ProtocolError("not a data frame")
    (sequence,) = SEQUENCE.unpack_from(frame.payload)
    return sequence, frame.payload[SEQUENCE.size :]


def decode_json(frame: Frame) -> dict[str, object]:
    """Parse the JSON object of a control frame."""
    try:
        body = json.loads(frame.payload)
    except ValueError as error:
        raise ProtocolError("malformed control frame") from error
    if not isinstance(body, dict):
        raise ProtocolError("malformed control frame")
    return body


class FrameDecoder:
    """Incremental parser: feed received bytes, get whole frames."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[Frame]:
        """Append ``data`` and return every complete frame; raises on a violation."""
        self._buffer += data
        frames: list[Frame] = []
        while len(self._buffer) >= HEADER.size:
            raw_type, length = HEADER.unpack_from(self._buffer)
            try:
                frame_type = FrameType(raw_type)
            except ValueError as error:
                raise ProtocolError("unknown frame type") from error
            limit = MAX_FRAME_BYTES if frame_type is FrameType.DATA else MAX_JSON_BYTES
            if length > limit:
                raise ProtocolError("frame too large")
            end = HEADER.size + length
            if len(self._buffer) < end:
                break
            frames.append(Frame(frame_type, bytes(self._buffer[HEADER.size : end])))
            del self._buffer[:end]
        return frames


class SequenceCounter:
    """Expected sequence numbers of one direction."""

    def __init__(self) -> None:
        self._next = 0

    def take(self) -> int:
        """The sequence number to send next."""
        value = self._next
        self._next = (self._next + 1) % SEQUENCE_MODULUS
        return value

    def expect(self, received: int) -> None:
        """Verify an incoming sequence number; a gap or repeat is a protocol violation."""
        if received != self._next:
            raise ProtocolError("input out of sequence")
        self._next = (self._next + 1) % SEQUENCE_MODULUS
