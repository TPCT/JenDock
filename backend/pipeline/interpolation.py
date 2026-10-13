"""Decoding of string bodies into literal/variable parts, and variable resolution rules.

Nothing here evaluates anything: a variable stays a ``VariablePart`` for later substitution.
"""

from __future__ import annotations

import re
from bisect import bisect_left
from dataclasses import dataclass

from backend.pipeline.ast import StringPart, TextPart, VariablePart, VarSource
from backend.pipeline.errors import PipelineSyntaxError, Span, syntax_error
from backend.pipeline.lexer import Quote

BUILTIN_VARIABLES = frozenset(
    {"BRANCH_NAME", "GIT_COMMIT", "BUILD_NUMBER", "ENVIRONMENT", "PROJECT"}
)

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", "'": "'", '"': '"', "$": "$"}
_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_BARE = re.compile(_NAME)
_BRACED = re.compile(rf"(?:(?P<source>env|params)\.)?(?P<name>{_NAME})")


def decode_string(source: str, body: str, start: Span, quote: Quote) -> tuple[StringPart, ...]:
    """Decode the raw ``body`` of a string token starting at ``start``."""
    return _Decoder(source, body, start, quote).run()


class _Decoder:
    def __init__(self, source: str, body: str, start: Span, quote: Quote) -> None:
        self._source = source
        self._body = body
        self._start = start
        self._interpolate = quote.interpolates
        self._pos = 0
        self._newlines = [i for i, char in enumerate(body) if char == "\n"]
        self._text: list[str] = []
        self._parts: list[StringPart] = []

    def run(self) -> tuple[StringPart, ...]:
        while self._pos < len(self._body):
            char = self._body[self._pos]
            if char == "\\":
                self._escape()
            elif char == "$" and self._interpolate:
                self._dollar()
            else:
                self._text.append(char)
                self._pos += 1
        self._flush()
        return tuple(self._parts)

    def _span_at(self, index: int) -> Span:
        before = bisect_left(self._newlines, index)
        if before == 0:
            return Span(self._start.line, self._start.column + index)
        return Span(self._start.line + before, index - self._newlines[before - 1])

    def _error(self, message: str, index: int) -> PipelineSyntaxError:
        return syntax_error(self._source, message, self._span_at(index))

    def _flush(self) -> None:
        if self._text:
            self._parts.append(TextPart("".join(self._text)))
            self._text = []

    def _escape(self) -> None:
        char = self._body[self._pos + 1 : self._pos + 2]
        if char == "\n":
            self._pos += 2
            return
        if char not in _ESCAPES:
            raise self._error(f"unknown escape sequence '\\{char}'", self._pos)
        self._text.append(_ESCAPES[char])
        self._pos += 2

    def _dollar(self) -> None:
        index = self._pos
        if self._body.startswith("{", index + 1):
            self._braced(index)
            return
        match = _BARE.match(self._body, index + 1)
        if match is None:
            raise self._error("lone '$'; write \\$ for a literal dollar sign", index)
        self._reject_property_access(match.end(), index)
        self._add(VarSource.BARE, match.group(), index)
        self._pos = match.end()

    def _reject_property_access(self, end: int, index: int) -> None:
        if self._body.startswith(".", end) and _BARE.match(self._body, end + 1):
            raise self._error("ambiguous '$NAME.' property access; write ${NAME} instead", index)

    def _braced(self, index: int) -> None:
        close = self._body.find("}", index + 2)
        if close < 0:
            raise self._error("unterminated '${'", index)
        content = self._body[index + 2 : close].strip()
        match = _BRACED.fullmatch(content)
        if match is None:
            self._add(VarSource.EXPRESSION, content, index)
        else:
            source = VarSource(match.group("source") or VarSource.BARE.value)
            self._add(source, match.group("name"), index)
        self._pos = close + 1

    def _add(self, source: VarSource, name: str, index: int) -> None:
        self._flush()
        self._parts.append(VariablePart(source, name, self._span_at(index)))


@dataclass(frozen=True, slots=True)
class VariableScope:
    """Names a variable may refer to at some point of the pipeline."""

    env: frozenset[str]
    params: frozenset[str]
    secrets: frozenset[str]

    def with_env(
        self, names: frozenset[str], secrets: frozenset[str] = frozenset()
    ) -> VariableScope:
        """A scope that additionally knows ``names`` (and which of them are secrets)."""
        return VariableScope(self.env | names, self.params, self.secrets | secrets)


def variable_problem(part: VariablePart, scope: VariableScope) -> str | None:
    """Return why ``part`` is not allowed in ``scope``, or ``None`` when it resolves."""
    if part.source is VarSource.EXPRESSION:
        return (
            f"unsupported expression '${{{part.name}}}': only ${{NAME}}, ${{env.NAME}} and "
            "${params.NAME} are supported"
        )
    if part.source is VarSource.PARAMS:
        known = part.name in scope.params
        return None if known else f"unknown parameter 'params.{part.name}'"
    known = part.name in scope.env or part.name in BUILTIN_VARIABLES
    if known:
        return None
    return f"unknown variable '{part.name}'; define it in an environment section"
