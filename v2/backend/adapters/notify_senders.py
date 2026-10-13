"""Sending notifications: HTTP webhooks and SMTP, behind small ports so tests never touch a
network. Failures become ``DeliveryError`` with a message that never contains a URL, a
credential or a response body."""

from __future__ import annotations

import json
import smtplib
import ssl
import urllib.error
import urllib.request
from collections.abc import Mapping
from datetime import datetime
from email.message import EmailMessage
from typing import Protocol

from backend.adapters.notify_format import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
    email_message,
    signature,
    slack_body,
    webhook_body,
)
from backend.domain.notifications import ChannelKind, EventPayload, Security

USER_AGENT = "Jendock-Notifier"


class DeliveryError(Exception):
    """A delivery attempt failed; ``str(error)`` is safe to store and show."""


class HttpPoster(Protocol):
    """POSTs a body and returns the status code (redirects are not followed)."""

    def post(self, url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> int:
        """Raise ``DeliveryError`` when the request cannot be made."""


class Mailer(Protocol):
    """Sends one email through the configured SMTP server."""

    def send(self, config: Mapping[str, object], message: EmailMessage, timeout: float) -> None:
        """Raise ``DeliveryError`` on failure."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


class UrllibPoster:
    """The real HTTP port: standard library, no redirects, body never read."""

    def post(self, url: str, body: bytes, headers: Mapping[str, str], timeout: float) -> int:
        """POST ``body``; HTTP error statuses are returned, not raised."""
        if not url.lower().startswith(("http://", "https://")):
            raise DeliveryError("the URL is not http(s)")
        request = urllib.request.Request(  # noqa: S310 - scheme checked above
            url, data=body, headers={"User-Agent": USER_AGENT, **headers}, method="POST"
        )
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=timeout) as response:
                return int(response.status)
        except urllib.error.HTTPError as error:
            return int(error.code)
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise DeliveryError(f"request failed ({type(error).__name__})") from error


class SmtpMailer:
    """The real mail port, over ``smtplib``."""

    def send(self, config: Mapping[str, object], message: EmailMessage, timeout: float) -> None:
        """Connect, secure the connection as configured, authenticate, send."""
        host = str(config["host"])
        port = int(str(config["port"]))
        security = str(config.get("security", Security.STARTTLS.value))
        context = ssl.create_default_context()
        try:
            smtp: smtplib.SMTP = (
                smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
                if security == Security.TLS.value
                else smtplib.SMTP(host, port, timeout=timeout)
            )
            with smtp:
                if security == Security.STARTTLS.value:
                    smtp.starttls(context=context)
                if "username" in config:
                    smtp.login(str(config["username"]), str(config["password"]))
                smtp.send_message(message)
        except smtplib.SMTPAuthenticationError as error:
            raise DeliveryError("SMTP authentication failed") from error
        except smtplib.SMTPResponseException as error:
            raise DeliveryError(f"SMTP error {error.smtp_code}") from error
        except (smtplib.SMTPException, OSError, ssl.SSLError) as error:
            raise DeliveryError(f"SMTP failed ({type(error).__name__})") from error


class Deliverer:
    """Formats a payload for a channel kind and sends it."""

    def __init__(self, poster: HttpPoster, mailer: Mailer, origin: str, timeout: float) -> None:
        self._poster = poster
        self._mailer = mailer
        self._origin = origin
        self._timeout = timeout

    def deliver(
        self,
        kind: ChannelKind,
        config: Mapping[str, object],
        payload: EventPayload,
        delivery_id: int,
        now: datetime,
    ) -> None:
        """Send one message; raise ``DeliveryError`` when it was not accepted."""
        if kind is ChannelKind.EMAIL:
            recipients = [str(item) for item in _list(config.get("to_addresses"))]
            message = email_message(payload, self._origin, str(config["from_address"]), recipients)
            self._mailer.send(config, message, self._timeout)
            return
        if kind is ChannelKind.SLACK:
            body = json.dumps(slack_body(payload, self._origin)).encode()
            headers = {"Content-Type": "application/json"}
            self._post(str(config["webhook_url"]), body, headers)
            return
        body = json.dumps(webhook_body(payload, self._origin, delivery_id, now)).encode()
        headers = {
            "Content-Type": "application/json",
            EVENT_HEADER: payload.event.value,
            DELIVERY_HEADER: str(delivery_id),
        }
        signing = config.get("signing_secret")
        if isinstance(signing, str) and signing:
            headers[SIGNATURE_HEADER] = signature(signing, body)
        self._post(str(config["url"]), body, headers)

    def _post(self, url: str, body: bytes, headers: Mapping[str, str]) -> None:
        status = self._poster.post(url, body, headers, self._timeout)
        if not 200 <= status < 300:
            raise DeliveryError(f"the endpoint answered HTTP {status}")


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []
