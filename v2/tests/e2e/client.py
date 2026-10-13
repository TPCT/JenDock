"""Clients of the public interfaces: the HTTP API (session + CSRF), SSE, MCP and the terminal.

The stack runs in production mode (a ``__Host-`` Secure cookie and an https public origin), but
the test reaches it over plain loopback http, so the session cookie is carried by hand in a
``Cookie`` header and every unsafe request sets ``Origin`` itself, like a browser would.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
from websockets.sync.client import connect

from tests.e2e.stack import PUBLIC_ORIGIN

SSE_TIMEOUT = 600.0


@dataclass(slots=True)
class SseEvent:
    """One server-sent event."""

    name: str
    data: str

    def json(self) -> Any:
        return json.loads(self.data)


@dataclass(slots=True)
class Api:
    """A logged-in browser session (or, with ``bearer``, an API token) against the stack."""

    base_url: str
    csrf: str = ""
    cookie: str = ""
    bearer: str = ""
    http: httpx.Client = field(default_factory=lambda: httpx.Client(timeout=60.0))

    # --- auth ---------------------------------------------------------------------

    def login(self, username: str, password: str) -> None:
        """Log in the way the web UI does and keep cookie and CSRF token."""
        response = self.http.post(
            f"{self.base_url}/api/auth/login",
            json={"username": username, "password": password},
            headers={"Origin": PUBLIC_ORIGIN},
        )
        assert response.status_code == 200, response.text
        raw = response.headers.get_list("set-cookie")
        assert raw, "login did not set a session cookie"
        pair = raw[0].split(";", 1)[0]
        assert pair.startswith("__Host-session="), pair
        self.cookie = pair
        self.csrf = response.json()["csrf_token"]

    def headers(self, *, unsafe: bool) -> dict[str, str]:
        """Authentication headers for one request."""
        if self.bearer:
            return {"Authorization": f"Bearer {self.bearer}"}
        headers = {"Cookie": self.cookie}
        if unsafe:
            headers.update({"Origin": PUBLIC_ORIGIN, "X-CSRF-Token": self.csrf})
        return headers

    # --- requests -----------------------------------------------------------------

    def request(
        self, method: str, path: str, *, expect: int | tuple[int, ...] = 200, **kwargs: Any
    ) -> Any:
        """One API call; asserts the status and returns the decoded JSON (or ``None``)."""
        unsafe = method.upper() not in {"GET", "HEAD"}
        response = self.http.request(
            method, f"{self.base_url}{path}", headers=self.headers(unsafe=unsafe), **kwargs
        )
        allowed = (expect,) if isinstance(expect, int) else expect
        assert response.status_code in allowed, (
            f"{method} {path} -> {response.status_code}: {response.text[:600]}"
        )
        return response.json() if response.content else None

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, body: object | None = None, **kwargs: Any) -> Any:
        return self.request("POST", path, json=body, **kwargs)

    def put(self, path: str, body: object, **kwargs: Any) -> Any:
        return self.request("PUT", path, json=body, **kwargs)

    def patch(self, path: str, body: object, **kwargs: Any) -> Any:
        return self.request("PATCH", path, json=body, **kwargs)

    # --- server-sent events -------------------------------------------------------

    def events(self, path: str, timeout: float = SSE_TIMEOUT) -> Iterator[SseEvent]:
        """Yield the events of an SSE endpoint until it ends or ``timeout`` passes."""
        deadline = time.monotonic() + timeout
        name = ""
        with self.http.stream(
            "GET",
            f"{self.base_url}{path}",
            headers=self.headers(unsafe=False),
            timeout=httpx.Timeout(30.0, read=timeout),
        ) as response:
            assert response.status_code == 200, response.read()
            for line in response.iter_lines():
                if time.monotonic() > deadline:
                    raise TimeoutError(f"no end event from {path} within {timeout}s")
                if line.startswith("event: "):
                    name = line.removeprefix("event: ")
                elif line.startswith("data: "):
                    yield SseEvent(name, line.removeprefix("data: "))

    def follow_run(
        self, run_id: int, timeout: float = SSE_TIMEOUT
    ) -> tuple[dict[str, Any], list[SseEvent]]:
        """Follow a run over SSE until it ends; returns the last status snapshot and all events."""
        seen: list[SseEvent] = []
        last: dict[str, Any] = {}
        for event in self.events(f"/api/runs/{run_id}/events", timeout):
            seen.append(event)
            if event.name == "status":
                last = event.json()
            if event.name == "end":
                break
        assert last, "the event stream carried no status"
        return last, seen

    def run_log(self, run: Mapping[str, Any]) -> str:
        """The concatenated step logs of a run (through the log endpoint)."""
        text = []
        for stage in run["stages"]:
            for step in stage["steps"]:
                offset = 0
                while True:
                    chunk = self.get(
                        f"/api/runs/{run['id']}/steps/{step['id']}/log?offset={offset}"
                    )
                    text.append(chunk["text"])
                    offset = chunk["next_offset"]
                    if chunk["complete"] or not chunk["text"]:
                        break
        return "".join(text)


def sign_webhook(secret: str, body: bytes) -> str:
    """The ``X-Hub-Signature-256`` header value GitHub would send."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def send_push(
    api: Api, secret: str, *, repository: str, branch: str, after: str, before: str = "0" * 40
) -> httpx.Response:
    """Deliver a signed ``push`` event the way GitHub does (no session, no CSRF)."""
    payload = {
        "ref": f"refs/heads/{branch}",
        "before": before,
        "after": after,
        "repository": {"full_name": repository},
        "pusher": {"name": "e2e"},
    }
    body = json.dumps(payload).encode()
    return httpx.post(
        f"{api.base_url}/api/github/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": str(uuid.uuid4()),
            "X-Hub-Signature-256": sign_webhook(secret, body),
        },
        timeout=30.0,
    )


class Mcp:
    """A minimal MCP client for the stateless streamable-HTTP endpoint (JSON-RPC over POST)."""

    def __init__(self, base_url: str, token: str, host: str) -> None:
        self._url = f"{base_url}/mcp"
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Host": host,
        }
        self._id = 0

    def call(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """One JSON-RPC request; returns the whole response object."""
        self._id += 1
        body = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        response = httpx.post(self._url, json=body, headers=self._headers, timeout=60.0)
        assert response.status_code == 200, (
            f"{method}: {response.status_code} {response.text[:500]}"
        )
        if response.headers.get("content-type", "").startswith("text/event-stream"):
            data = [line[6:] for line in response.text.splitlines() if line.startswith("data: ")]
            return json.loads(data[-1])
        return response.json()

    def initialize(self) -> Any:
        reply = self.call(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "e2e", "version": "1"},
            },
        )
        assert "result" in reply, reply
        return reply["result"]

    def tools(self) -> Any:
        return self.call("tools/list")["result"]["tools"]

    def tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Call a tool; returns the JSON-RPC ``result`` (check ``isError``)."""
        reply = self.call("tools/call", {"name": name, "arguments": arguments})
        assert "result" in reply, reply
        return reply["result"]


class Terminal:
    """One terminal WebSocket session, as the browser's xterm.js would speak it."""

    def __init__(self, api: Api, ticket: str, websocket_path: str) -> None:
        url = api.base_url.replace("http://", "ws://") + websocket_path
        self._stack = contextlib.ExitStack()
        self._socket = self._stack.enter_context(
            connect(
                url,
                additional_headers={"Origin": PUBLIC_ORIGIN, "Cookie": api.cookie},
                open_timeout=15,
            )
        )
        try:
            self._socket.send(
                json.dumps({"type": "auth", "ticket": ticket, "rows": 24, "cols": 80})
            )
            ready = json.loads(self._socket.recv(timeout=15))
            assert ready["type"] == "ready", ready
        except BaseException:
            self._stack.close()
            raise
        self.terminal_id = ready["terminal_id"]

    def send(self, text: str) -> None:
        self._socket.send(text.encode())

    def read_until(self, needle: str, timeout: float = 20.0) -> str:
        """Collect output until ``needle`` appears (after any echo of the command itself)."""
        deadline = time.monotonic() + timeout
        seen = ""
        while time.monotonic() < deadline:
            try:
                message = self._socket.recv(timeout=max(0.1, deadline - time.monotonic()))
            except TimeoutError:
                break
            if isinstance(message, bytes):
                seen += message.decode(errors="replace")
                if needle in seen:
                    return seen
        raise AssertionError(f"{needle!r} not seen in terminal output: {seen[-500:]!r}")

    def close(self) -> dict[str, Any] | None:
        """Ask the shell to end; returns the ``exit`` message when the server sends one."""
        self._socket.send(json.dumps({"type": "close"}))
        exit_message = None
        with contextlib.suppress(Exception):  # the server closes the socket after the exit message
            for message in self._socket:
                if isinstance(message, str):
                    parsed = json.loads(message)
                    if parsed.get("type") == "exit":
                        exit_message = parsed
        self._stack.close()
        return exit_message
