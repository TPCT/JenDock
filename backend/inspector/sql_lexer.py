"""A small SQL tokenizer for the console guard (PostgreSQL, MySQL and MariaDB lexical rules).

It only has to be *conservative*: whatever it classifies as a string or comment must be a
string or comment for the server too, otherwise forbidden words could hide inside it. Where
the dialects differ (backslash escapes, ``--`` comments, ``#`` comments, dollar quoting,
executable comments) the rules of the server dialect are followed, and constructs that cannot
be modelled safely are rejected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from backend.inspector.model import Engine, ErrorCode, InspectorError

_WORD_START = re.compile(r"[^\W\d]", re.UNICODE)
_WORD = re.compile(r"[\w$]+", re.UNICODE)
_NUMBER = re.compile(r"\d+(?:\.\d*)?(?:[eE][+-]?\d+)?|\.\d+(?:[eE][+-]?\d+)?")
_DOLLAR_TAG = re.compile(r"\$(?:[^\W\d][\w]*)?\$", re.UNICODE)
_SPACE = " \t\r\n\f\v"


class Kind(StrEnum):
    """Token categories."""

    WORD = "word"
    QUOTED = "quoted"
    STRING = "string"
    NUMBER = "number"
    PUNCT = "punct"


@dataclass(frozen=True, slots=True)
class Token:
    """One token with its source span; ``text`` of strings and quoted names is the content."""

    kind: Kind
    text: str
    start: int
    end: int

    @property
    def upper(self) -> str:
        """Upper-cased text (words only make sense here)."""
        return self.text.upper()


def _reject(message: str) -> InspectorError:
    return InspectorError(ErrorCode.REJECTED, message)


class _Scanner:
    def __init__(self, sql: str, engine: Engine) -> None:
        self.sql = sql
        self.pos = 0
        self.mysql = engine in {Engine.MYSQL, Engine.MARIADB}
        self.tokens: list[Token] = []

    def run(self) -> list[Token]:
        while self.pos < len(self.sql):
            self._step()
        return self.tokens

    def _peek(self, offset: int = 0) -> str:
        index = self.pos + offset
        return self.sql[index] if index < len(self.sql) else ""

    def _add(self, kind: Kind, text: str, start: int) -> None:
        self.tokens.append(Token(kind, text, start, self.pos))

    def _step(self) -> None:
        char = self._peek()
        if char in _SPACE:
            self.pos += 1
        elif self._is_line_comment():
            self._skip_line_comment()
        elif char == "/" and self._peek(1) == "*":
            self._skip_block_comment()
        elif char == "'":
            self._quoted_string("'", Kind.STRING, escapes=self.mysql)
        elif char == '"':
            self._double_quoted()
        elif char == "`" and self.mysql:
            self._quoted_string("`", Kind.QUOTED, escapes=False)
        elif char == "$" and not self.mysql and self._dollar_quote():
            return
        else:
            self._word_number_or_punct()

    def _is_line_comment(self) -> bool:
        char = self._peek()
        if char == "#":
            return self.mysql
        if char == "-" and self._peek(1) == "-":
            if not self.mysql:
                return True
            # MySQL needs whitespace or a control character after the dashes.
            following = self._peek(2)
            return following == "" or following in _SPACE or ord(following) < 32
        return False

    def _skip_line_comment(self) -> None:
        end = self.sql.find("\n", self.pos)
        self.pos = len(self.sql) if end == -1 else end + 1

    def _skip_block_comment(self) -> None:
        if (
            self.mysql
            and self._peek(2) in {"!", "M"}
            and self.sql.startswith(("/*!", "/*M!"), self.pos)
        ):
            raise _reject("executable comments (/*! ... */) are not allowed")
        depth = 0
        while self.pos < len(self.sql):
            pair = self.sql[self.pos : self.pos + 2]
            if pair == "/*" and (not self.mysql or depth == 0):
                depth += 1
                self.pos += 2
            elif pair == "*/":
                depth -= 1
                self.pos += 2
                if depth == 0:
                    return
            else:
                self.pos += 1
        raise _reject("unterminated comment")

    def _quoted_string(self, quote: str, kind: Kind, *, escapes: bool) -> None:
        start = self.pos
        self.pos += 1
        chars: list[str] = []
        while self.pos < len(self.sql):
            char = self.sql[self.pos]
            if escapes and char == "\\" and self.pos + 1 < len(self.sql):
                chars.append(self.sql[self.pos : self.pos + 2])
                self.pos += 2
            elif char == quote:
                if self._peek(1) == quote:
                    chars.append(quote)
                    self.pos += 2
                else:
                    self.pos += 1
                    self._add(kind, "".join(chars), start)
                    return
            else:
                chars.append(char)
                self.pos += 1
        raise _reject("unterminated string or quoted identifier")

    def _double_quoted(self) -> None:
        # MySQL: a string (backslash escapes); PostgreSQL: a quoted identifier.
        if self.mysql:
            self._quoted_string('"', Kind.STRING, escapes=True)
        else:
            self._quoted_string('"', Kind.QUOTED, escapes=False)

    def _dollar_quote(self) -> bool:
        match = _DOLLAR_TAG.match(self.sql, self.pos)
        if match is None:
            return False
        tag = match.group(0)
        end = self.sql.find(tag, match.end())
        if end == -1:
            raise _reject("unterminated dollar-quoted string")
        start = self.pos
        body = self.sql[match.end() : end]
        self.pos = end + len(tag)
        self._add(Kind.STRING, body, start)
        return True

    def _word_number_or_punct(self) -> None:
        start = self.pos
        char = self._peek()
        number = _NUMBER.match(self.sql, self.pos)
        if number is not None and (char.isdigit() or char == "."):
            self.pos = number.end()
            self._add(Kind.NUMBER, number.group(0), start)
        elif _WORD_START.match(char) or char == "_":
            self._word()
        else:
            self.pos += 1
            self._add(Kind.PUNCT, char, start)

    def _word(self) -> None:
        start = self.pos
        match = _WORD.match(self.sql, self.pos)
        if match is None:
            raise _reject("invalid character")
        text = match.group(0)
        self.pos = match.end()
        following = self._peek()
        if text.upper() in {"E", "N", "B", "X"} and following == "'":
            # Prefixed literal: E'..' uses backslash escapes in PostgreSQL.
            self._quoted_string("'", Kind.STRING, escapes=self.mysql or text.upper() == "E")
            return
        if text.upper() == "U" and following == "&" and self._peek(1) in {'"', "'"}:
            raise _reject('unicode escapes (U&"...") are not allowed')
        self._add(Kind.WORD, text, start)


def tokenize(sql: str, engine: Engine) -> list[Token]:
    """Split ``sql`` into tokens (comments dropped); raises ``InspectorError`` when it cannot be
    understood safely (unterminated strings or comments, executable comments, unicode escapes).
    """
    if "\0" in sql:
        raise _reject("statement contains a NUL character")
    return _Scanner(sql, engine).run()
