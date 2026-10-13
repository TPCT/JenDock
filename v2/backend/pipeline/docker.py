"""Docker-related grammar: image references and the allowlist for agent ``args``."""

from __future__ import annotations

import re
import shlex

_COMPONENT = r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*"
_LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_HOST = rf"{_LABEL}(?:\.{_LABEL})*(?::[0-9]+)?"
_TAG = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}"
_DIGEST = r"sha256:[a-f0-9]{64}"
_REFERENCE = re.compile(rf"(?:{_HOST}/)?{_COMPONENT}(?:/{_COMPONENT})*(?::{_TAG})?(?:@{_DIGEST})?")
_MAX_REFERENCE = 255
_ENV_ARG = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
_MEMORY = re.compile(r"[0-9]+[bkmgBKMG]?")
_CPUS = re.compile(r"[0-9]+(?:\.[0-9]+)?")

ALLOWED_ARGS_HELP = "--network none, -e NAME=value, --memory N, --cpus N"


def is_external_reference(image: str) -> bool:
    """Whether ``image`` is a valid reference that pins a tag or digest (``node:22``)."""
    if len(image) > _MAX_REFERENCE or _REFERENCE.fullmatch(image) is None:
        return False
    return "@" in image or ":" in image.rsplit("/", 1)[-1]


def check_docker_args(text: str) -> str | None:
    """Return why the agent ``args`` are rejected, or ``None`` when all are allowed."""
    try:
        tokens = shlex.split(text)
    except ValueError as error:
        return f"cannot parse docker args: {error}"
    return _ArgChecker(tokens).run()


class _ArgChecker:
    def __init__(self, tokens: list[str]) -> None:
        self._tokens = tokens
        self._pos = 0

    def run(self) -> str | None:
        while self._pos < len(self._tokens):
            problem = self._one()
            if problem:
                return problem
        return None

    def _one(self) -> str | None:
        token = self._tokens[self._pos]
        self._pos += 1
        flag, equals, inline = token.partition("=")
        rule = _RULES.get(flag if token.startswith("--") else token)
        if rule is None:
            return _not_allowed(flag if token.startswith("-") else token)
        value = inline if equals and token.startswith("--") else self._take()
        if value is None:
            return f"docker arg '{flag}' needs a value"
        return rule(value)

    def _take(self) -> str | None:
        if self._pos >= len(self._tokens):
            return None
        self._pos += 1
        return self._tokens[self._pos - 1]


def _not_allowed(flag: str) -> str:
    return f"docker arg '{flag}' is not allowed; allowed: {ALLOWED_ARGS_HELP}"


def _network(value: str) -> str | None:
    return None if value == "none" else "docker arg '--network' only allows 'none'"


def _env(value: str) -> str | None:
    return None if _ENV_ARG.fullmatch(value) else "docker arg '-e' needs NAME=value"


def _memory(value: str) -> str | None:
    return None if _MEMORY.fullmatch(value) else "docker arg '--memory' needs a size like 512m"


def _cpus(value: str) -> str | None:
    return None if _CPUS.fullmatch(value) else "docker arg '--cpus' needs a number"


_RULES = {"--network": _network, "-e": _env, "--memory": _memory, "--cpus": _cpus}
