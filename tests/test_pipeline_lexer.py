import pytest

from backend.pipeline.ast import StringPart, TextPart, VariablePart
from backend.pipeline.errors import PipelineSyntaxError, Span
from backend.pipeline.interpolation import decode_string
from backend.pipeline.lexer import MAX_DEPTH, MAX_INPUT_BYTES, Quote, TokenKind, tokenize

K = TokenKind


def kinds(text: str) -> list[TokenKind]:
    return [t.kind for t in tokenize(text)]


def fail(text: str) -> PipelineSyntaxError:
    with pytest.raises(PipelineSyntaxError) as info:
        tokenize(text)
    return info.value


def test_basic_tokens_and_positions() -> None:
    tokens = tokenize("sh 'x'\n  retry(3) { true; null }")
    assert [(t.kind, t.span) for t in tokens[:3]] == [
        (K.IDENT, Span(1, 1)),
        (K.STRING, Span(1, 4)),
        (K.NEWLINE, Span(1, 7)),
    ]
    assert tokens[3].span == Span(2, 3)
    assert kinds("a = [1, 2]: , ;") == [
        K.IDENT, K.EQUALS, K.LBRACKET, K.INT, K.COMMA, K.INT, K.RBRACKET, K.COLON, K.COMMA,
        K.SEMI, K.EOF,
    ]  # fmt: skip
    assert kinds("true false null x") == [K.BOOL, K.BOOL, K.NULL, K.IDENT, K.EOF]


def test_newlines_collapse_and_comments_vanish() -> None:
    text = "a // c1\n\n\n/* b\n c */ b\n"
    assert kinds(text) == [K.IDENT, K.NEWLINE, K.IDENT, K.NEWLINE, K.EOF]
    assert tokenize(text)[2].span == Span(5, 7)


def test_crlf_is_normalised() -> None:
    tokens = tokenize("a\r\nb")
    assert tokens[2].span == Span(2, 1)


@pytest.mark.parametrize(
    ("text", "quote", "body"),
    [
        ("'a b'", Quote.SINGLE, "a b"),
        ('"a $b"', Quote.DOUBLE, "a $b"),
        ("'''l1\nl2'''", Quote.TRIPLE_SINGLE, "l1\nl2"),
        ('"""x "q" y"""', Quote.TRIPLE_DOUBLE, 'x "q" y'),
        (r"'it\'s'", Quote.SINGLE, r"it\'s"),
    ],
)
def test_string_kinds(text: str, quote: Quote, body: str) -> None:
    token = tokenize(text)[0]
    assert (token.kind, token.quote, token.text) == (K.STRING, quote, body)


def test_multiline_string_updates_following_positions() -> None:
    tokens = tokenize("'''a\nb\nc''' x")
    assert tokens[1].span == Span(3, 6)


@pytest.mark.parametrize(
    ("text", "position"),
    [
        ("sh 'abc", Span(1, 4)),
        ("a\n  \"abc\nd'", Span(2, 3)),
        ("x '''abc\n'", Span(1, 3)),
        ("'abc\\", Span(1, 1)),
        ("a\n/* open\n", Span(2, 1)),
    ],
)
def test_unterminated_reports_start(text: str, position: Span) -> None:
    error = fail(text)
    assert error.span == position
    assert "unterminated" in error.message


def test_error_excerpt_has_caret() -> None:
    error = fail("echo 'abc")
    assert str(error) == "line 1, column 6: unterminated string\n    echo 'abc\n         ^"


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("@Library('x')", "@Library"),
        ("a.b", None),
        ("12ab", "integers"),
        ("1.5", "integers"),
        ("x \0", "NUL"),
        ("x + y", "unexpected character '+'"),
        ("-1", "unexpected character '-'"),
    ],
)
def test_unsupported_characters(text: str, fragment: str | None) -> None:
    if fragment is None:
        assert K.DOT in kinds(text)
        return
    assert fragment in fail(text).message


def test_size_limit() -> None:
    assert tokenize("a" * 10)
    error = fail("a" * (MAX_INPUT_BYTES + 1))
    assert "exceeds" in error.message
    assert tokenize("// " + "é" * 100)  # multi-byte counted in bytes, well below the limit
    assert "exceeds" in fail("é" * (MAX_INPUT_BYTES // 2 + 1)).message


def test_nesting_limit() -> None:
    assert tokenize("{" * MAX_DEPTH + "}" * MAX_DEPTH)
    error = fail("{" * (MAX_DEPTH + 1))
    assert error.span == Span(1, MAX_DEPTH + 1)
    assert "deeper" in error.message
    assert tokenize("{}" * 1000)  # depth is not a count of all brackets


def decode(body: str, quote: Quote = Quote.DOUBLE) -> tuple[StringPart, ...]:
    return decode_string(body, body, Span(1, 2), quote)


def test_escapes_decode() -> None:
    parts = decode(r"a\n\t\\\'\"\$b")
    assert parts == (TextPart("a\n\t\\'\"$b"),)


def test_escape_errors_have_exact_position() -> None:
    with pytest.raises(PipelineSyntaxError) as info:
        decode("ab\\q")
    assert info.value.span == Span(1, 4)
    with pytest.raises(PipelineSyntaxError) as info:
        decode_string("x", "l1\nab\\q", Span(5, 3), Quote.TRIPLE_DOUBLE)
    assert info.value.span == Span(6, 3)


def test_single_quoted_does_not_interpolate() -> None:
    parts = decode("$HOME ${X}", Quote.SINGLE)
    assert parts == (TextPart("$HOME ${X}"),)


@pytest.mark.parametrize("body", ["cost$", "a $ b", "${unclosed", "$NAME.txt"])
def test_bad_dollar_is_syntax_error(body: str) -> None:
    with pytest.raises(PipelineSyntaxError):
        decode(body)


def test_variable_part_positions() -> None:
    parts = decode("ab ${X} $Y")
    spans = [p.span for p in parts if isinstance(p, VariablePart)]
    assert spans == [Span(1, 5), Span(1, 10)]


def test_integer_size_is_bounded() -> None:
    assert tokenize("999999999")
    assert "at most 9 digits" in fail("1" * 5000).message
