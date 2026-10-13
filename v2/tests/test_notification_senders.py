"""The real HTTP and SMTP ports: a loopback server and a fake ``smtplib`` (no external service)."""

from __future__ import annotations

import smtplib
import threading
from collections.abc import Iterator
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from backend.adapters.notify_senders import DeliveryError, SmtpMailer, UrllibPoster


class _Handler(BaseHTTPRequestHandler):
    seen: list[tuple[str, bytes, str]] = []  # noqa: RUF012 - shared with the test on purpose

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.seen.append((self.path, body, self.headers.get("User-Agent", "")))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
            return
        self.send_response(500 if self.path == "/boom" else 204)
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        return


@pytest.fixture
def server() -> Iterator[str]:
    _Handler.seen = []
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


def test_poster_sends_the_body_and_returns_the_status_without_following_redirects(
    server: str,
) -> None:
    poster = UrllibPoster()
    assert poster.post(f"{server}/ok", b'{"a":1}', {"Content-Type": "application/json"}, 5) == 204
    assert poster.post(f"{server}/boom", b"{}", {}, 5) == 500
    assert poster.post(f"{server}/redirect", b"{}", {}, 5) == 302  # not followed
    paths = [path for path, _, _ in _Handler.seen]
    assert paths == ["/ok", "/boom", "/redirect"]
    assert _Handler.seen[0][1] == b'{"a":1}' and _Handler.seen[0][2] == "Jendock-Notifier"


def test_poster_failures_name_the_exception_type_never_the_url() -> None:
    poster = UrllibPoster()
    url = "http://127.0.0.1:1/TOPSECRETPATH"
    with pytest.raises(DeliveryError) as raised:
        poster.post(url, b"{}", {}, 2)
    assert str(raised.value).startswith("request failed (")
    assert "TOPSECRETPATH" not in str(raised.value) and "127.0.0.1" not in str(raised.value)
    with pytest.raises(DeliveryError, match="not http"):
        poster.post("file:///etc/passwd", b"", {}, 1)


class FakeSmtp:
    """Records the conversation; class attributes drive failures."""

    instances: list[FakeSmtp] = []  # noqa: RUF012
    fail_login: Exception | None = None
    fail_send: Exception | None = None

    def __init__(self, host: str, port: int, timeout: float = 0, context: object = None) -> None:
        self.host, self.port, self.timeout, self.tls = host, port, timeout, context is not None
        self.log: list[str] = []
        self.instances.append(self)

    def __enter__(self) -> FakeSmtp:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def starttls(self, context: object = None) -> None:
        self.log.append("starttls")

    def login(self, user: str, password: str) -> None:
        if self.fail_login:
            raise self.fail_login
        self.log.append(f"login {user}")

    def send_message(self, message: EmailMessage) -> None:
        if self.fail_send:
            raise self.fail_send
        self.log.append(f"send {message['To']}")


class FakeSmtpSsl(FakeSmtp):
    """The implicit TLS variant."""


@pytest.fixture(autouse=True)
def fake_smtplib(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeSmtp.instances = []
    FakeSmtp.fail_login = None
    FakeSmtp.fail_send = None
    monkeypatch.setattr(smtplib, "SMTP", FakeSmtp)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSmtpSsl)


def message() -> EmailMessage:
    mail = EmailMessage()
    mail["To"] = "ops@example.test"
    mail.set_content("hi")
    return mail


BASE = {"host": "smtp.example.test", "port": 587}


def test_starttls_logs_in_and_sends() -> None:
    SmtpMailer().send(
        {**BASE, "security": "starttls", "username": "u", "password": "p"}, message(), 5
    )
    [smtp] = FakeSmtp.instances
    assert not isinstance(smtp, FakeSmtpSsl)
    assert smtp.log == ["starttls", "login u", "send ops@example.test"]


def test_implicit_tls_uses_the_ssl_client_and_none_skips_everything() -> None:
    SmtpMailer().send({**BASE, "port": 465, "security": "tls"}, message(), 5)
    SmtpMailer().send({**BASE, "security": "none"}, message(), 5)
    tls, plain = FakeSmtp.instances
    assert isinstance(tls, FakeSmtpSsl) and tls.log == ["send ops@example.test"]
    assert not isinstance(plain, FakeSmtpSsl) and plain.log == ["send ops@example.test"]


def test_smtp_failures_become_short_safe_errors() -> None:
    config = {**BASE, "security": "none", "username": "u", "password": "hunter2"}
    FakeSmtp.fail_login = smtplib.SMTPAuthenticationError(535, b"bad credentials hunter2")
    with pytest.raises(DeliveryError) as auth:
        SmtpMailer().send(config, message(), 5)
    assert str(auth.value) == "SMTP authentication failed"
    FakeSmtp.fail_login = None
    FakeSmtp.fail_send = smtplib.SMTPRecipientsRefused({"x": (550, b"no such user hunter2")})
    with pytest.raises(DeliveryError) as refused:
        SmtpMailer().send(config, message(), 5)
    assert "hunter2" not in str(refused.value)
    FakeSmtp.fail_send = smtplib.SMTPDataError(554, b"rejected hunter2")
    with pytest.raises(DeliveryError) as data:
        SmtpMailer().send(config, message(), 5)
    assert str(data.value) == "SMTP error 554"
    FakeSmtp.fail_send = OSError("connection reset by hunter2.example")
    with pytest.raises(DeliveryError) as down:
        SmtpMailer().send(config, message(), 5)
    assert str(down.value) == "SMTP failed (OSError)"
