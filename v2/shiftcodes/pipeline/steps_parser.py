"""Parsing of step calls in paren and command syntax."""

from __future__ import annotations

from shiftcodes.pipeline.ast import Argument, StepCall
from shiftcodes.pipeline.cursor import TokenCursor
from shiftcodes.pipeline.lexer import Token, TokenKind
from shiftcodes.pipeline.steps import Registry
from shiftcodes.pipeline.values_parser import ValueParser

_GROOVY_KEYWORDS = frozenset(
    {
        "assert",
        "break",
        "case",
        "catch",
        "class",
        "continue",
        "def",
        "else",
        "finally",
        "for",
        "if",
        "import",
        "new",
        "package",
        "return",
        "switch",
        "throw",
        "try",
        "while",
    }
)
SCRIPT_MESSAGE = "script blocks (free Groovy) are not supported; use sh steps"


class StepParser:
    """Parses calls such as ``sh 'x'``, ``retry(3) { ... }`` and ``deploy service: 'a'``."""

    def __init__(self, cursor: TokenCursor, values: ValueParser) -> None:
        self._cur = cursor
        self._values = values

    def steps_block(self, registry: Registry) -> tuple[StepCall, ...]:
        """Parse ``{ step* }``."""
        return tuple(self._cur.block(lambda: self.call(registry)))

    def call(self, registry: Registry) -> StepCall:
        """Parse one call; positional arguments are named using ``registry``."""
        name = self._cur.expect(TokenKind.IDENT, "expected a step name")
        self._reject_groovy(name)
        args, parenthesised = self._arguments()
        body = None
        if self._cur.check(TokenKind.LBRACE):
            if args and not parenthesised:
                raise self._cur.fail(
                    "put the arguments in parentheses before a block: name(args) { ... }",
                    self._cur.peek().span,
                )
            body = self.steps_block(registry)
        return StepCall(name.text, _name_positionals(registry, name.text, args), body, name.span)

    def arguments(self) -> list[Argument]:
        """Parse an argument list in paren or command syntax (possibly empty)."""
        return self._arguments()[0]

    def _arguments(self) -> tuple[list[Argument], bool]:
        if self._cur.check(TokenKind.LPAREN):
            return self._parenthesised(), True
        if not self._values.starts_value():
            return [], False
        args = [self._values.argument()]
        while self._cur.accept(TokenKind.COMMA):
            self._cur.skip_newlines()
            args.append(self._values.argument())
        return args, False

    def _parenthesised(self) -> list[Argument]:
        self._cur.advance()
        args: list[Argument] = []
        self._cur.skip_newlines()
        while not self._cur.check(TokenKind.RPAREN):
            args.append(self._values.argument())
            self._cur.skip_newlines()
            if not self._cur.accept(TokenKind.COMMA):
                break
            self._cur.skip_newlines()
        self._cur.expect(TokenKind.RPAREN, "expected ',' or ')'")
        return args

    def _reject_groovy(self, name: Token) -> None:
        if name.text == "script":
            raise self._cur.fail(SCRIPT_MESSAGE, name.span)
        if name.text in _GROOVY_KEYWORDS:
            message = f"Groovy '{name.text}' is not supported; only step calls are allowed"
            raise self._cur.fail(message, name.span)
        if self._cur.check(TokenKind.EQUALS):
            message = "assignments are not supported; only step calls are allowed"
            raise self._cur.fail(message, name.span)


def _name_positionals(registry: Registry, step: str, args: list[Argument]) -> tuple[Argument, ...]:
    definition = registry.get(step)
    names = list(definition.positional) if definition else []
    result: list[Argument] = []
    for arg in args:
        if arg.name is None and names:
            arg = Argument(names.pop(0), arg.value, arg.span)  # noqa: PLW2901
        result.append(arg)
    return tuple(result)
