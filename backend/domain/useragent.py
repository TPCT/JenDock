"""A short human summary of a User-Agent header for the sessions list."""

from __future__ import annotations

import re

# Order matters: Edge and Opera also say Chrome, Chrome also says Safari.
_BROWSERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("Edge", re.compile(r"Edg(?:e|A|iOS)?/(\d+)")),
    ("Opera", re.compile(r"(?:OPR|Opera)/(\d+)")),
    ("Firefox", re.compile(r"(?:Firefox|FxiOS)/(\d+)")),
    ("Chrome", re.compile(r"(?:Chrome|CriOS)/(\d+)")),
    ("Safari", re.compile(r"Version/(\d+)[\d.]* .*Safari/")),
    ("curl", re.compile(r"curl/(\d+)")),
)
_SYSTEMS: tuple[tuple[str, str], ...] = (
    ("Windows", "Windows"),
    ("iPhone", "iOS"),
    ("iPad", "iPadOS"),
    ("Android", "Android"),
    ("Mac OS X", "macOS"),
    ("CrOS", "ChromeOS"),
    ("Linux", "Linux"),
)


def summarize_user_agent(user_agent: str | None) -> str:
    """``"Firefox 130 on Linux"`` for typical browsers, ``"Unknown client"`` otherwise."""
    if not user_agent:
        return "Unknown client"
    browser = next(
        (f"{name} {match.group(1)}" for name, rx in _BROWSERS if (match := rx.search(user_agent))),
        None,
    )
    system = next((label for needle, label in _SYSTEMS if needle in user_agent), None)
    if browser and system:
        return f"{browser} on {system}"
    return browser or system or "Unknown client"
