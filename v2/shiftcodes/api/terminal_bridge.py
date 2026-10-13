"""Bridges one browser WebSocket to the worker's terminal socket.

Browser to API: binary messages are terminal input (at most 64 KiB each), text messages are JSON
controls (``resize``, ``close``, ``ping``). API to browser: binary messages are terminal output;
text messages are JSON events (``ready``, ``exit``, ``error``, ``pong``). Input and output are
relayed and never stored or logged; the API only keeps what the framed protocol needs.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable
from pathlib import Path

from starlette.concurrency import run_in_threadpool
from starlette.websockets import WebSocket, WebSocketState

from shiftcodes.api.terminal_tickets import PrincipalKind, TicketGrant
from shiftcodes.terminal.protocol import (
    MAX_DATA_BYTES,
    Frame,
    FrameDecoder,
    FrameType,
    ProtocolError,
    SequenceCounter,
    decode_data,
    decode_json,
    encode_data,
    encode_frame,
    encode_json,
)

CONNECT_TIMEOUT = 5.0
READY_TIMEOUT = 15.0
MAX_INPUT_MESSAGE = 64 * 1024
MAX_CONTROL_MESSAGE = 4096
SESSION_CHECK_SECONDS = 15.0
SESSION_TOUCH_SECONDS = 300.0
READ_CHUNK = 65536
CLOSE_NORMAL = 1000
CLOSE_POLICY = 1008
CLOSE_TOO_BIG = 1009
CLOSE_UNAVAILABLE = 1013
CLOSE_SESSION_ENDED = 4401
SessionCheck = Callable[[bool], bool]


class TerminalRejectedError(Exception):
    """The worker refused to open the terminal; the message is safe to show."""


class TerminalBridge:
    """Runs one terminal connection until either side ends it."""

    def __init__(  # noqa: PLR0913, PLR0917 - the bridge's fixed context
        self,
        socket_path: Path,
        websocket: WebSocket,
        grant: TicketGrant,
        session_check: SessionCheck | None,
        rows: int,
        cols: int,
    ) -> None:
        self._path = socket_path
        self._ws = websocket
        self._grant = grant
        self._check = session_check  # ``check(touch)`` -> whether the browser session lives
        self._rows = rows
        self._cols = cols
        self._decoder = FrameDecoder()
        self._output = SequenceCounter()
        self._input = SequenceCounter()
        self._last_touch = time.monotonic()
        self._backlog: list[Frame] = []

    async def run(self) -> None:
        """Connect to the worker, open or reattach, then relay until done."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self._path)), CONNECT_TIMEOUT
            )
        except (OSError, TimeoutError):
            await self._close(CLOSE_UNAVAILABLE, "terminal service unavailable")
            return
        try:
            await self._relay(reader, writer)
        except TerminalRejectedError as error:
            await self._send_event({"type": "error", "message": str(error)})
            await self._close(CLOSE_POLICY, "terminal refused")
        except (ProtocolError, OSError):
            await self._close(CLOSE_UNAVAILABLE, "terminal connection failed")
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    async def _relay(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        grant = self._grant
        request: dict[str, object] = {
            "slug": grant.slug,
            "env": grant.env.value,
            "container_id": grant.container_id,
            "actor": {"type": grant.actor.type.value, "id": grant.actor.id},
            "rows": self._rows,
            "cols": self._cols,
        }
        if grant.terminal_id:
            request["attach"] = grant.terminal_id
        writer.write(encode_json(FrameType.OPEN, request))
        await writer.drain()
        terminal_id = await asyncio.wait_for(self._await_ready(reader), READY_TIMEOUT)
        await self._send_event({"type": "ready", "terminal_id": terminal_id})
        tasks = [
            asyncio.create_task(self._upstream(writer)),
            asyncio.create_task(self._downstream(reader)),
            asyncio.create_task(self._watch_session()),
        ]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._close(CLOSE_NORMAL, "")

    async def _await_ready(self, reader: asyncio.StreamReader) -> str:
        """Wait for READY; frames that arrive in the same chunk are kept for the relay."""
        while True:
            data = await reader.read(READ_CHUNK)
            if not data:
                raise ProtocolError("worker closed the connection")
            frames = self._decoder.feed(data)
            if not frames:
                continue
            first = frames[0]
            if first.type is FrameType.ERROR:
                message = decode_json(first).get("message")
                raise TerminalRejectedError(str(message or "terminal refused"))
            if first.type is not FrameType.READY:
                raise ProtocolError("unexpected frame before ready")
            value = decode_json(first).get("terminal_id")
            if not isinstance(value, str):
                raise ProtocolError("malformed ready frame")
            self._backlog = frames[1:]
            return value

    async def _upstream(self, writer: asyncio.StreamWriter) -> None:
        """Browser to worker. Returning means the browser went away (the worker detaches)."""
        while True:
            message = await self._ws.receive()
            if message["type"] == "websocket.disconnect":
                return
            data, text = message.get("bytes"), message.get("text")
            if data is not None:
                if len(data) > MAX_INPUT_MESSAGE:
                    await self._close(CLOSE_TOO_BIG, "input message too large")
                    return
                await self._send_input(writer, data)
            elif text is not None and not await self._control(writer, text):
                return

    async def _send_input(self, writer: asyncio.StreamWriter, data: bytes) -> None:
        for start in range(0, len(data), MAX_DATA_BYTES):
            chunk = data[start : start + MAX_DATA_BYTES]
            writer.write(encode_data(self._input.take(), chunk))
        await writer.drain()
        await self._touch_session()

    async def _control(self, writer: asyncio.StreamWriter, text: str) -> bool:
        """Handle a JSON control message; ``False`` ends the connection."""
        if len(text) > MAX_CONTROL_MESSAGE:
            await self._close(CLOSE_TOO_BIG, "control message too large")
            return False
        try:
            body = json.loads(text)
        except ValueError:
            return True
        kind = body.get("type") if isinstance(body, dict) else None
        if kind == "resize":
            rows, cols = body.get("rows"), body.get("cols")
            if isinstance(rows, int) and isinstance(cols, int):
                writer.write(encode_json(FrameType.RESIZE, {"rows": rows, "cols": cols}))
                await writer.drain()
        elif kind == "close":
            writer.write(encode_frame(FrameType.CLOSE))
            await writer.drain()
            return True  # the worker answers with EXIT, which ends the downstream task
        elif kind == "ping":
            await self._send_event({"type": "pong"})
        return True

    async def _downstream(self, reader: asyncio.StreamReader) -> None:
        """Worker to browser. Returning means the terminal ended or the worker went away."""
        frames, self._backlog = self._backlog, []
        while True:
            for frame in frames:
                if await self._deliver(frame):
                    return
            data = await reader.read(READ_CHUNK)
            if not data:
                return
            frames = self._decoder.feed(data)

    async def _deliver(self, frame: Frame) -> bool:
        """Forward one frame to the browser; ``True`` when the terminal is over."""
        if frame.type is FrameType.DATA:
            sequence, payload = decode_data(frame)
            self._output.expect(sequence)
            await self._ws.send_bytes(payload)
        elif frame.type is FrameType.EXIT:
            body = decode_json(frame)
            await self._send_event(
                {"type": "exit", "code": body.get("code"), "reason": body.get("reason")}
            )
            return True
        elif frame.type is FrameType.ERROR:
            message = decode_json(frame).get("message")
            await self._send_event({"type": "error", "message": str(message)})
            return True
        return False

    async def _watch_session(self) -> None:
        """End the terminal connection when the browser session ends (logout, expiry)."""
        check = self._check
        if self._grant.kind is not PrincipalKind.SESSION or check is None:
            await asyncio.Event().wait()  # nothing to watch: wait until cancelled
            return
        while True:
            await asyncio.sleep(SESSION_CHECK_SECONDS)
            if not await run_in_threadpool(check, False):
                await self._close(CLOSE_SESSION_ENDED, "session ended")
                return

    async def _touch_session(self) -> None:
        """Typing counts as activity: extend the session idle timer now and then."""
        now = time.monotonic()
        if self._check is not None and now - self._last_touch >= SESSION_TOUCH_SECONDS:
            self._last_touch = now
            await run_in_threadpool(self._check, True)

    async def _send_event(self, body: dict[str, object]) -> None:
        if self._ws.application_state is WebSocketState.CONNECTED:
            with contextlib.suppress(RuntimeError, OSError):
                await self._ws.send_text(json.dumps(body))

    async def _close(self, code: int, reason: str) -> None:
        if self._ws.application_state is WebSocketState.CONNECTED:
            with contextlib.suppress(RuntimeError, OSError):
                await self._ws.close(code=code, reason=reason)
