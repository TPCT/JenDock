"""Token cursor with the small helpers every parser class shares."""

from __future__ import annotations

from collections.abc import Callable

from shiftcodes.pipeline.errors import PipelineSyntaxError, Span, syntax_error
from shiftcodes.pipeline.lexer import Token, TokenKind

_SEPARATORS = {TokenKind.NEWLINE, TokenKind.SEMI}


class TokenCursor:
    """Position in a token list plus error construction."""

    def __init__(self, source: str, tokens: list[Token]) -> None:
        self.source = source
        self._tokens = tokens
        self._pos = 0

    def peek(self, offset: int = 0) -> Token:
        """The token ``offset`` ahead (EOF past the end)."""
        index = min(self._pos + offset, len(self._tokens) - 1)
        return self._tokens[index]

    def advance(self) -> Token:
        """Consume and return the current token."""
        token = self.peek()
        if token.kind is not TokenKind.EOF:
            self._pos += 1
        return token

    def check(self, kind: TokenKind, offset: int = 0) -> bool:
        """Whether the token ``offset`` ahead has ``kind``."""
        return self.peek(offset).kind is kind

    def check_ident(self, text: str) -> bool:
        """Whether the current token is the identifier ``text``."""
        token = self.peek()
        return token.kind is TokenKind.IDENT and token.text == text

    def accept(self, kind: TokenKind) -> Token | None:
        """Consume the current token when it has ``kind``."""
        return self.advance() if self.check(kind) else None

    def expect(self, kind: TokenKind, message: str | None = None) -> Token:
        """Consume a token of ``kind`` or fail."""
        if not self.check(kind):
            found = self.peek()
            text = message or f"expected {kind.value}"
            raise self.fail(f"{text}, found {_describe(found)}", found.span)
        return self.advance()

    def fail(self, message: str, span: Span) -> PipelineSyntaxError:
        """An error positioned at ``span``."""
        return syntax_error(self.source, message, span)

    def skip_newlines(self) -> None:
        """Skip newline tokens (inside parentheses and brackets)."""
        while self.check(TokenKind.NEWLINE):
            self.advance()

    def skip_separators(self) -> None:
        """Skip newlines and semicolons between statements."""
        while self.peek().kind in _SEPARATORS:
            self.advance()

    def skip_newlines_before_brace(self) -> None:
        """Skip newlines when the next real token is ``{`` (allowed before section braces)."""
        offset = 0
        while self.check(TokenKind.NEWLINE, offset):
            offset += 1
        if offset and self.check(TokenKind.LBRACE, offset):
            self.skip_newlines()

    def block[T](self, parse_item: Callable[[], T]) -> list[T]:
        """Parse ``{ item* }`` where items are separated by newlines or ``;``."""
        self.skip_newlines_before_brace()
        opening = self.expect(TokenKind.LBRACE, "expected '{'")
        items: list[T] = []
        self.skip_separators()
        while not self.check(TokenKind.RBRACE):
            if self.check(TokenKind.EOF):
                raise self.fail("unclosed '{'; missing '}'", opening.span)
            items.append(parse_item())
            self._end_statement()
            self.skip_separators()
        self.advance()
        return items

    def _end_statement(self) -> None:
        if self.peek().kind not in {*_SEPARATORS, TokenKind.RBRACE}:
            found = self.peek()
            raise self.fail(f"expected a new line or ';', found {_describe(found)}", found.span)


def _describe(token: Token) -> str:
    if token.kind in {TokenKind.IDENT, TokenKind.INT, TokenKind.BOOL, TokenKind.NULL}:
        return f"'{token.text}'"
    if token.kind is TokenKind.STRING:
        return "a string"
    return token.kind.value
