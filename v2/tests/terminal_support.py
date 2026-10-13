"""A terminal server on a local ``/bin/sh`` (no Docker) and a blocking client for it."""

from __future__ import annotations

import os
import socket
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from shiftcodes.containers.base import EnvironmentTarget
from shiftcodes.domain.actor import Actor
from shiftcodes.domain.containers import EnvironmentKey
from shiftcodes.domain.enums import ActorType, EnvironmentName
from shiftcodes.domain.errors import DomainError
from shiftcodes.terminal.protocol import (
    Frame,
    FrameDecoder,
    FrameType,
    SequenceCounter,
    decode_data,
    decode_json,
    encode_data,
    encode_frame,
    encode_json,
)
from shiftcodes.terminal.server import OpenRequest, TerminalServer
from shiftcodes.terminal.session import (
    OpenSpec,
    TerminalLimits,
    TerminalManager,
    TerminalSession,
)

CONTAINER = "c" * 64
TARGET = EnvironmentTarget(
    EnvironmentKey("demo", EnvironmentName.DEVELOPMENT), 1, "demo-dev", ("/x/compose.yml",)
)
USER = Actor(ActorType.USER, "1")


@dataclass
class Recorder:
    """Terminal listener that remembers metadata events."""

    events: list[tuple[str, str]] = field(default_factory=list)

    def opened(self, session: TerminalSession) -> None:
        """Remember an open."""
        self.events.append(("open", session.id))

    def closed(
        self, session: TerminalSession, reason: str, duration: float, code: int | None
    ) -> None:
        """Remember a close with its reason."""
        self.events.append(("close", reason))


class ShellOpener:
    """Opens ``/bin/sh`` locally; ``missing`` makes the container unknown."""

    def __init__(self) -> None:
        self.missing = False

    def prepare(self, request: OpenRequest) -> OpenSpec:
        """A local shell instead of ``docker exec``."""
        if self.missing:
            raise DomainError("container not found")
        env = {"PATH": os.environ["PATH"], "PS1": "$ ", "TERM": "dumb"}
        return OpenSpec(
            ["/bin/sh"],
            env,
            request.rows,
            request.cols,
            request.actor.to_actor(),
            TARGET,
            request.container_id,
            "demo-dev-web-1",
            "web",
        )


@dataclass
class Running:
    """A started server with its manager, recorder and opener."""

    path: Path
    manager: TerminalManager
    recorder: Recorder
    opener: ShellOpener


@contextmanager
def terminal_server(socket_dir: Path, limits: TerminalLimits | None = None) -> Iterator[Running]:
    """Run a terminal server on a short temp socket path."""
    recorder, opener = Recorder(), ShellOpener()
    manager = TerminalManager(limits or TerminalLimits(), recorder)
    path = socket_dir / "t.sock"
    server = TerminalServer(path, manager, opener)
    manager.start()
    server.start()
    try:
        yield Running(path, manager, recorder, opener)
    finally:
        server.stop()
        manager.shutdown()


class Client:
    """Speaks the framed protocol over a blocking socket."""

    def __init__(self, path: Path) -> None:
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.settimeout(0.1)
        self.socket.connect(str(path))
        self.decoder = FrameDecoder()
        self.frames: list[Frame] = []
        self.sequence = SequenceCounter()
        self.output = b""
        self.exit: dict[str, object] | None = None
        self.error: str | None = None
        self.terminal_id: str | None = None

    def open(self, **extra: object) -> None:
        """Send OPEN for the default container and actor."""
        body: dict[str, object] = {
            "slug": "demo",
            "env": "development",
            "container_id": CONTAINER,
            "actor": {"type": "user", "id": "1"},
            **extra,
        }
        self.socket.sendall(encode_json(FrameType.OPEN, body))

    def send(self, text: str) -> None:
        """Type ``text``."""
        self.socket.sendall(encode_data(self.sequence.take(), text.encode()))

    def send_raw_sequence(self, sequence: int, data: bytes) -> None:
        """A DATA frame with an explicit sequence number."""
        self.socket.sendall(encode_data(sequence, data))

    def resize(self, rows: int, cols: int) -> None:
        """Change the window size."""
        self.socket.sendall(encode_json(FrameType.RESIZE, {"rows": rows, "cols": cols}))

    def close_terminal(self) -> None:
        """Ask the worker to end the terminal."""
        self.socket.sendall(encode_frame(FrameType.CLOSE))

    def pump(self, until: bytes | None = None, timeout: float = 5.0) -> None:
        """Read frames until EXIT/ERROR/EOF, or until ``until`` appeared in the output."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if until is not None and until in self.output:
                return
            if self.exit is not None or self.error is not None:
                return
            try:
                data = self.socket.recv(65536)
            except TimeoutError:
                continue
            if not data:
                return
            for frame in self.decoder.feed(data):
                self._take(frame)

    def _take(self, frame: Frame) -> None:
        if frame.type is FrameType.READY:
            self.terminal_id = str(decode_json(frame)["terminal_id"])
        elif frame.type is FrameType.DATA:
            self.output += decode_data(frame)[1]
        elif frame.type is FrameType.EXIT:
            self.exit = decode_json(frame)
        elif frame.type is FrameType.ERROR:
            self.error = str(decode_json(frame)["message"])

    def drop(self) -> None:
        """Close the socket without a CLOSE frame (a browser that went away)."""
        self.socket.close()


def wait_for(condition, timeout: float = 5.0) -> bool:  # type: ignore[no-untyped-def]
    """Poll ``condition`` until true or the timeout passes."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False
