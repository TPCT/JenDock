"""Template text of the editing model: strings whose ``${NAME}`` parts are variables.

``encode`` and ``decode`` are inverse for every string the parser can produce. ``decode`` never
fails: text that is not a variable reference (a lone ``$``, an unterminated ``${``) stays literal
so a half-typed template can still be rendered and diagnosed.
"""

from __future__ import annotations

import re

from shiftcodes.pipeline.ast import StringPart, TextPart, VariablePart, VarSource
from shiftcodes.pipeline.errors import Span

NO_SPAN = Span(1, 1)
_NAME = r"[A-Za-z_][A-Za-z0-9_]*"
_BARE = re.compile(_NAME)
_BRACED = re.compile(rf"(?:(?P<source>env|params)\.)?(?P<name>{_NAME})")


def encode(parts: tuple[StringPart, ...]) -> str:
    """The template text of ``parts``: variables as ``${...}``, ``\\`` and ``$`` escaped."""
    return "".join(_encode_part(part) for part in parts)


def _encode_part(part: StringPart) -> str:
    if isinstance(part, TextPart):
        return part.text.replace("\\", "\\\\").replace("$", "\\$")
    if part.source in {VarSource.ENV, VarSource.PARAMS}:
        return f"${{{part.source.value}.{part.name}}}"
    return f"${{{part.name}}}"


def decode(text: str) -> tuple[StringPart, ...]:
    """The parts of template ``text`` (adjacent literal text is merged, empty text dropped)."""
    return _Decoder(text).run()


class _Decoder:
    def __init__(self, text: str) -> None:
        self._text = text
        self._pos = 0
        self._literal: list[str] = []
        self._parts: list[StringPart] = []

    def run(self) -> tuple[StringPart, ...]:
        while self._pos < len(self._text):
            char = self._text[self._pos]
            if char == "\\" and self._text[self._pos + 1 : self._pos + 2] in {"\\", "$"}:
                self._literal.append(self._text[self._pos + 1])
                self._pos += 2
            elif char == "$" and self._variable():
                continue
            else:
                self._literal.append(char)
                self._pos += 1
        self._flush()
        return tuple(self._parts)

    def _flush(self) -> None:
        if self._literal:
            self._parts.append(TextPart("".join(self._literal)))
            self._literal = []

    def _variable(self) -> bool:
        """Consume a variable at the cursor (a ``$``); ``False`` leaves the ``$`` as text."""
        if self._text.startswith("{", self._pos + 1):
            return self._braced()
        match = _BARE.match(self._text, self._pos + 1)
        if match is None:
            return False
        self._add(VarSource.BARE, match.group(), match.end())
        return True

    def _braced(self) -> bool:
        close = self._text.find("}", self._pos + 2)
        if close < 0:
            return False
        content = self._text[self._pos + 2 : close].strip()
        match = _BRACED.fullmatch(content)
        if match is None:
            self._add(VarSource.EXPRESSION, content, close + 1)
        else:
            source = VarSource(match.group("source") or VarSource.BARE.value)
            self._add(source, match.group("name"), close + 1)
        return True

    def _add(self, source: VarSource, name: str, end: int) -> None:
        self._flush()
        self._parts.append(VariablePart(source, name, NO_SPAN))
        self._pos = end
