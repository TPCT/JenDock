"""Parsing of literal values and call arguments."""

from __future__ import annotations

from collections.abc import Callable

from shiftcodes.pipeline.ast import (
    Argument,
    BoolValue,
    IntValue,
    ListValue,
    MapEntry,
    MapValue,
    StringValue,
    Value,
)
from shiftcodes.pipeline.cursor import TokenCursor
from shiftcodes.pipeline.interpolation import decode_string
from shiftcodes.pipeline.lexer import Token, TokenKind

_VALUE_START = {
    TokenKind.STRING,
    TokenKind.INT,
    TokenKind.BOOL,
    TokenKind.NULL,
    TokenKind.LBRACKET,
    TokenKind.IDENT,
}


class ValueParser:
    """Parses strings, numbers, booleans, lists, maps and argument lists."""

    def __init__(self, cursor: TokenCursor) -> None:
        self._cur = cursor

    def string(self, token: Token) -> StringValue:
        """Decode a STRING token into parts."""
        assert token.quote is not None  # noqa: S101 - STRING tokens always have a quote
        parts = decode_string(self._cur.source, token.text, token.body_span, token.quote)
        return StringValue(parts, token.span)

    def string_literal(self, what: str) -> StringValue:
        """Parse a string token or fail with ``what`` in the message."""
        token = self._cur.expect(TokenKind.STRING, f"expected {what}")
        return self.string(token)

    def plain_string(self, what: str) -> tuple[str, StringValue]:
        """Parse a string without variables; returns its text and node."""
        value = self.string_literal(what)
        text = value.plain
        if text is None:
            raise self._cur.fail(f"{what} must not contain variables", value.span)
        return text, value

    def value(self) -> Value:
        """Parse one value."""
        token = self._cur.peek()
        if token.kind is TokenKind.STRING:
            return self.string(self._cur.advance())
        if token.kind is TokenKind.INT:
            return IntValue(int(self._cur.advance().text), token.span)
        if token.kind is TokenKind.BOOL:
            return BoolValue(self._cur.advance().text == "true", token.span)
        if token.kind is TokenKind.LBRACKET:
            return self._collection()
        raise self._cur.fail(_value_error(token), token.span)

    def starts_value(self) -> bool:
        """Whether the current token can begin an argument."""
        return self._cur.peek().kind in _VALUE_START

    def argument(self) -> Argument:
        """Parse ``name: value`` or a positional value."""
        token = self._cur.peek()
        if token.kind is TokenKind.IDENT and self._cur.check(TokenKind.COLON, 1):
            self._cur.advance()
            self._cur.advance()
            return Argument(token.text, self.value(), token.span)
        return Argument(None, self.value(), token.span)

    def _collection(self) -> ListValue | MapValue:
        opening = self._cur.advance()
        self._cur.skip_newlines()
        if self._cur.accept(TokenKind.COLON):
            self._cur.expect(TokenKind.RBRACKET, "expected ']' to close the empty map [:]")
            return MapValue((), opening.span)
        if self._cur.accept(TokenKind.RBRACKET):
            return ListValue((), opening.span)
        if self._starts_entry():
            return MapValue(tuple(self._items(self._entry)), opening.span)
        return ListValue(tuple(self._items(self.value)), opening.span)

    def _starts_entry(self) -> bool:
        first = self._cur.peek()
        keyed = first.kind in {TokenKind.IDENT, TokenKind.STRING}
        return keyed and self._cur.check(TokenKind.COLON, 1)

    def _entry(self) -> MapEntry:
        token = self._cur.advance()
        if token.kind not in {TokenKind.IDENT, TokenKind.STRING}:
            raise self._cur.fail("map keys must be identifiers or strings", token.span)
        key = token.text if token.kind is TokenKind.IDENT else self._plain_key(token)
        self._cur.expect(TokenKind.COLON, "expected ':' after the map key")
        return MapEntry(key, self.value(), token.span)

    def _plain_key(self, token: Token) -> str:
        text = self.string(token).plain
        if text is None:
            raise self._cur.fail("map keys must not contain variables", token.span)
        return text

    def _items[T](self, parse_item: Callable[[], T]) -> list[T]:
        items = [parse_item()]
        self._cur.skip_newlines()
        while self._cur.accept(TokenKind.COMMA):
            self._cur.skip_newlines()
            if self._cur.check(TokenKind.RBRACKET):
                break
            items.append(parse_item())
            self._cur.skip_newlines()
        self._cur.expect(TokenKind.RBRACKET, "expected ',' or ']'")
        return items


def _value_error(token: Token) -> str:
    if token.kind is TokenKind.NULL:
        return "null is not supported"
    if token.kind is TokenKind.IDENT:
        return f"'{token.text}' is not a value; variables and Groovy expressions are not supported"
    return f"expected a value, found {token.kind.value}"
