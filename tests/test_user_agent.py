"""Short summaries of User-Agent headers for the session list."""

import pytest

from backend.domain.useragent import summarize_user_agent

CASES = [
    (
        "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0",
        "Firefox 130 on Linux",
    ),
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36 Edg/126.0.2592.81",
        "Edge 126 on Windows",
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/17.5 Safari/605.1.15",
        "Safari 17 on macOS",
    ),
    (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
        "Safari 17 on iOS",
    ),
    (
        "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Mobile Safari/537.36",
        "Chrome 126 on Android",
    ),
    ("curl/8.5.0", "curl 8"),
    ("SomethingOdd/1.0", "Unknown client"),
    ("", "Unknown client"),
    (None, "Unknown client"),
]


@pytest.mark.parametrize(("agent", "expected"), CASES)
def test_summaries(agent: str | None, expected: str) -> None:
    assert summarize_user_agent(agent) == expected
