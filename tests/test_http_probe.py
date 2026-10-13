"""The deploy health-check probe, against a throwaway local HTTP server."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from backend.adapters.http_probe import USER_AGENT, UrllibProbe


class _Recorder(BaseHTTPRequestHandler):
    seen: list[str] = []  # noqa: RUF012 - shared by the handler instances of one test

    def do_GET(self) -> None:
        agent = self.headers.get("User-Agent", "")
        type(self).seen.append(agent)
        # Behave like a CDN that refuses urllib's default agent.
        status = 403 if agent.startswith("Python-urllib") else 200
        self.send_response(status)
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        return


@pytest.fixture
def server() -> Iterator[str]:
    _Recorder.seen = []
    httpd = HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_the_probe_identifies_itself_so_a_cdn_does_not_refuse_it(server: str) -> None:
    assert UrllibProbe().get_status(server, 5.0) == 200
    assert _Recorder.seen == [USER_AGENT]
