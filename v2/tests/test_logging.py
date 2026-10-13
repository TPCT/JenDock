import io
import json
import logging
from collections.abc import Iterator

import pytest

from shiftcodes.config import Settings
from shiftcodes.logging import REDACTED, SecretRedactor, configure_logging


@pytest.fixture
def stream() -> Iterator[io.StringIO]:
    yield io.StringIO()
    logging.getLogger().handlers.clear()


def _emit(
    stream: io.StringIO,
    secrets: list[str],
    msg: str,
    args: tuple[str, ...] = (),
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    redactor = configure_logging(Settings(), stream)
    for secret in secrets:
        redactor.register(secret)
    logging.getLogger("t").info(msg, *args, extra=extra)
    result: dict[str, object] = json.loads(stream.getvalue())
    return result


def test_json_output_shape(stream: io.StringIO) -> None:
    out = _emit(stream, [], msg="hello", extra={"run_id": 7})
    assert out["message"] == "hello"
    assert out["level"] == "INFO"
    assert out["run_id"] == 7


def test_registered_secret_masked_in_message_and_args(stream: io.StringIO) -> None:
    out = _emit(stream, ["s3cr3t-value"], msg="using %s now", args=("s3cr3t-value",))
    assert out["message"] == f"using {REDACTED} now"


def test_registered_secret_masked_in_extra_nested(stream: io.StringIO) -> None:
    extra: dict[str, object] = {"info": {"url": "http://h/abc123", "n": [1, "abc123"]}}
    out = _emit(stream, ["abc123"], msg="x", extra=extra)
    assert out["info"] == {"url": f"http://h/{REDACTED}", "n": [1, REDACTED]}


@pytest.mark.parametrize(
    "key", ["password", "token", "client_secret", "Authorization", "cookie", "api_key", "X_API_KEY"]
)
def test_sensitive_keys_masked(stream: io.StringIO, key: str) -> None:
    out = _emit(stream, [], msg="x", extra={key: "plain"})
    assert out[key] == REDACTED


def test_non_sensitive_extra_untouched(stream: io.StringIO) -> None:
    out = _emit(stream, [], msg="x", extra={"project": "demo"})
    assert out["project"] == "demo"


def test_empty_secret_is_ignored() -> None:
    redactor = SecretRedactor()
    redactor.register("")
    assert redactor.redact_text("abc") == "abc"


def test_longest_secret_masked_first() -> None:
    redactor = SecretRedactor()
    redactor.register("abc")
    redactor.register("abcdef")
    assert redactor.redact_text("abcdef") == REDACTED


def test_exception_traceback_is_redacted(stream: io.StringIO) -> None:
    redactor = configure_logging(Settings(), stream)
    redactor.register("hunter2")
    try:
        raise ValueError("bad password hunter2")
    except ValueError:
        logging.getLogger("t").exception("failed")
    out = json.loads(stream.getvalue())
    assert "hunter2" not in stream.getvalue()
    assert REDACTED in out["exception"]


def test_stack_info_is_redacted(stream: io.StringIO) -> None:
    redactor = configure_logging(Settings(), stream)
    redactor.register("hunter2")

    def leaky_frame(hunter2: str) -> None:
        logging.getLogger("t").info("x", stack_info=True)

    leaky_frame("v")
    assert "stack_info" in json.loads(stream.getvalue())
    redactor_text = redactor.redact_text("trace hunter2")
    assert redactor_text == f"trace {REDACTED}"
