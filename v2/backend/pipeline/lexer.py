"""Tokenizer for the supported Jenkinsfile subset."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from backend.pipeline.errors import PipelineSyntaxError, Span, syntax_error

MAX_INPUT_BYTES = 256 * 1024
MAX_DEPTH = 32
MAX_INT_DIGITS = 9

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_INT = re.compile(r"[0-9]+")
_OPENERS = "{(["
_CLOSERS = "})]"
_BLANK = " \t\f"


class TokenKind(StrEnum):
    """Kinds of tokens; punctuation tokens use their own character as text."""

    IDENT = "identifier"
    STRING = "string"
    INT = "integer"
    BOOL = "boolean"
    NULL = "null"
    LBRACE = "{"
    RBRACE = "}"
    LPAREN = "("
    RPAREN = ")"
    LBRACKET = "["
    RBRACKET = "]"
    COMMA = ","
    COLON = ":"
    EQUALS = "="
    SEMI = ";"
    DOT = "."
    NEWLINE = "newline"
    EOF = "end of input"


class Quote(StrEnum):
    """Delimiter of a string token."""

    SINGLE = "'"
    DOUBLE = '"'
    TRIPLE_SINGLE = "'''"
    TRIPLE_DOUBLE = '"""'

    @property
    def interpolates(self) -> bool:
        """Whether ``$`` introduces an interpolation."""
        return self in {Quote.DOUBLE, Quote.TRIPLE_DOUBLE}

    @property
    def multiline(self) -> bool:
        """Whether raw newlines are allowed in the body."""
        return len(self.value) == 3


@dataclass(frozen=True, slots=True)
class Token:
    """A token; for strings ``text`` is the raw (still escaped) body."""

    kind: TokenKind
    text: str
    span: Span
    quote: Quote | None = None

    @property
    def body_span(self) -> Span:
        """Position of the first body character of a string token."""
        width = len(self.quote.value) if self.quote else 0
        return Span(self.span.line, self.span.column + width)


_PUNCTUATION = {
    "{": TokenKind.LBRACE,
    "}": TokenKind.RBRACE,
    "(": TokenKind.LPAREN,
    ")": TokenKind.RPAREN,
    "[": TokenKind.LBRACKET,
    "]": TokenKind.RBRACKET,
    ",": TokenKind.COMMA,
    ":": TokenKind.COLON,
    "=": TokenKind.EQUALS,
    ";": TokenKind.SEMI,
    ".": TokenKind.DOT,
}
_KEYWORDS = {"true": TokenKind.BOOL, "false": TokenKind.BOOL, "null": TokenKind.NULL}
_QUOTES = (Quote.TRIPLE_SINGLE, Quote.TRIPLE_DOUBLE, Quote.SINGLE, Quote.DOUBLE)


def tokenize(source: str) -> list[Token]:
    """Return all tokens of ``source`` ending with an EOF token; CRLF is read as LF."""
    return _Lexer(source.replace("\r\n", "\n")).run()


class _Lexer:
    def __init__(self, source: str) -> None:
        self._source = source
        self._pos = 0
        self._line = 1
        self._line_start = 0
        self._depth = 0
        self._tokens: list[Token] = []

    def run(self) -> list[Token]:
        if len(self._source.encode()) > MAX_INPUT_BYTES:
            raise self._error(f"input exceeds {MAX_INPUT_BYTES} bytes", Span(1, 1))
        while self._pos < len(self._source):
            self._step()
        self._add(TokenKind.EOF, "", self._span())
        return self._tokens

    def _span(self) -> Span:
        return Span(self._line, self._pos - self._line_start + 1)

    def _error(self, message: str, span: Span) -> PipelineSyntaxError:
        return syntax_error(self._source, message, span)

    def _add(self, kind: TokenKind, text: str, span: Span, quote: Quote | None = None) -> None:
        self._tokens.append(Token(kind, text, span, quote))

    def _newline(self) -> None:
        span = self._span()
        self._pos += 1
        self._line += 1
        self._line_start = self._pos
        if self._tokens and self._tokens[-1].kind is not TokenKind.NEWLINE:
            self._add(TokenKind.NEWLINE, "\n", span)

    def _step(self) -> None:
        char = self._source[self._pos]
        if char == "\n":
            self._newline()
        elif char in _BLANK:
            self._pos += 1
        elif self._source.startswith(("//", "/*"), self._pos):
            self._comment()
        elif char in "'\"":
            self._string()
        elif char in _PUNCTUATION:
            self._punctuation(char)
        else:
            self._word(char)

    def _comment(self) -> None:
        if self._source.startswith("//", self._pos):
            end = self._source.find("\n", self._pos)
            self._pos = len(self._source) if end < 0 else end
            return
        span = self._span()
        end = self._source.find("*/", self._pos + 2)
        if end < 0:
            raise self._error("unterminated block comment", span)
        self._consume(self._source[self._pos : end + 2])

    def _consume(self, text: str) -> None:
        """Advance over ``text`` keeping line bookkeeping correct."""
        newlines = text.count("\n")
        if newlines:
            self._line += newlines
            self._line_start = self._pos + text.rindex("\n") + 1
        self._pos += len(text)

    def _punctuation(self, char: str) -> None:
        span = self._span()
        if char in _OPENERS:
            self._depth += 1
            if self._depth > MAX_DEPTH:
                raise self._error(f"nesting is deeper than {MAX_DEPTH} levels", span)
        elif char in _CLOSERS and self._depth > 0:
            self._depth -= 1
        self._add(_PUNCTUATION[char], char, span)
        self._pos += 1

    def _word(self, char: str) -> None:
        span = self._span()
        if number := _INT.match(self._source, self._pos):
            self._number(number, span)
        elif ident := _IDENT.match(self._source, self._pos):
            text = ident.group()
            self._add(_KEYWORDS.get(text, TokenKind.IDENT), text, span)
            self._pos = ident.end()
        else:
            raise self._error(_unexpected(char), span)

    def _number(self, match: re.Match[str], span: Span) -> None:
        end = match.end()
        following = self._source[end : end + 1]
        if following.isalnum() or following in {".", "_"}:
            raise self._error("only plain integers are supported as numbers", span)
        if len(match.group()) > MAX_INT_DIGITS:
            raise self._error(f"integers may have at most {MAX_INT_DIGITS} digits", span)
        self._add(TokenKind.INT, match.group(), span)
        self._pos = end

    def _string(self) -> None:
        span = self._span()
        quote = next(q for q in _QUOTES if self._source.startswith(q.value, self._pos))
        start = self._pos + len(quote.value)
        end = self._find_end(quote, start, span)
        body = self._source[start:end]
        self._add(TokenKind.STRING, body, span, quote)
        self._consume(self._source[self._pos : end + len(quote.value)])

    def _find_end(self, quote: Quote, start: int, span: Span) -> int:
        pos = start
        while pos < len(self._source):
            char = self._source[pos]
            if char == "\\":
                pos += 2
            elif self._source.startswith(quote.value, pos):
                return pos
            elif char == "\n" and not quote.multiline:
                break
            else:
                pos += 1
        raise self._error("unterminated string", span)


def _unexpected(char: str) -> str:
    if char == "@":
        return "annotations such as @Library are not supported"
    if char == "\0":
        return "NUL characters are not allowed"
    return f"unexpected character {char!r}; Groovy expressions are not supported"
