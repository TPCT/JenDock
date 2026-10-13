"""The console's statement guard: one read-only statement, parsed conservatively.

This is the first of two layers. The second one is the database itself: every session is
opened read-only, so a statement that slipped past this guard still cannot write.

Accepted: a single ``SELECT``, ``WITH``, ``SHOW`` or ``EXPLAIN`` statement (an optional final
``;`` is allowed). Rejected: anything else, chained statements, ``INTO`` (``OUTFILE``,
``DUMPFILE``, ``SELECT INTO``), data-modifying keywords anywhere, locking reads,
``EXPLAIN ANALYZE`` (it executes the statement), executable comments, and a deny-list of
functions that sleep, take locks, read server files or change server state. The sleep functions
are refused outright rather than checked against the timeout: the statement timeout already
stops slow queries.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.inspector.model import MAX_STATEMENT_CHARS, Engine, ErrorCode, InspectorError
from backend.inspector.sql_lexer import Kind, Token, tokenize

ALLOWED_FIRST = frozenset({"SELECT", "WITH", "SHOW", "EXPLAIN"})
FORBIDDEN_WORDS = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "MERGE",
        "DROP",
        "ALTER",
        "CREATE",
        "TRUNCATE",
        "GRANT",
        "REVOKE",
        "INTO",
        "LOCK",
        "OUTFILE",
        "DUMPFILE",
    }
)
EXPLAIN_FORBIDDEN = frozenset({"ANALYZE", "ANALYSE"})
LOCKING_AFTER_FOR = frozenset({"SHARE", "NO", "KEY"})
FORBIDDEN_FUNCTION_PREFIXES = (
    "pg_sleep",
    "sleep",
    "benchmark",
    "get_lock",
    "release_",
    "is_free_lock",
    "is_used_lock",
    "master_pos_wait",
    "load_file",
    "lo_",
    "pg_read_",
    "pg_ls_",
    "pg_stat_file",
    "pg_stat_reset",
    "pg_terminate",
    "pg_cancel",
    "pg_reload",
    "pg_rotate",
    "pg_switch",
    "pg_create",
    "pg_drop",
    "pg_advisory",
    "pg_try_advisory",
    "pg_notify",
    "pg_promote",
    "pg_logical",
    "pg_replication",
    "pg_backup",
    "pg_start_backup",
    "pg_import_system",
    "dblink",
    "query_to_xml",
    "set_config",
    "nextval",
    "setval",
    "sys_",
)


@dataclass(frozen=True, slots=True)
class ValidatedStatement:
    """A statement that passed the guard; ``text`` has the optional final ``;`` removed."""

    text: str
    kind: str


def _reject(message: str) -> InspectorError:
    return InspectorError(ErrorCode.REJECTED, message)


def validate_statement(sql: str, engine: Engine) -> ValidatedStatement:
    """Return the statement if it is one read-only statement; raise ``InspectorError`` otherwise."""
    if len(sql) > MAX_STATEMENT_CHARS:
        raise _reject(f"statement is longer than {MAX_STATEMENT_CHARS} characters")
    tokens = tokenize(sql, engine)
    text, tokens = _strip_terminator(sql, tokens)
    if not tokens:
        raise _reject("statement is empty")
    kind = _first_keyword(tokens)
    if kind not in ALLOWED_FIRST:
        raise _reject("only SELECT, WITH, SHOW and EXPLAIN statements are allowed")
    _check_functions(tokens)
    if kind != "SHOW":
        _check_words(tokens, kind)
    return ValidatedStatement(text, kind)


def _strip_terminator(sql: str, tokens: list[Token]) -> tuple[str, list[Token]]:
    semicolons = [t for t in tokens if t.kind is Kind.PUNCT and t.text == ";"]
    if not semicolons:
        return sql.strip(), tokens
    last = tokens[-1]
    if len(semicolons) > 1 or last is not semicolons[0]:
        raise _reject("only one statement is allowed (found ';' inside the statement)")
    return sql[: last.start].strip(), tokens[:-1]


def _first_keyword(tokens: list[Token]) -> str:
    for token in tokens:
        if token.kind is Kind.PUNCT and token.text == "(":
            continue
        return token.upper if token.kind is Kind.WORD else ""
    return ""


def _check_words(tokens: list[Token], kind: str) -> None:
    words = [(i, t) for i, t in enumerate(tokens) if t.kind is Kind.WORD]
    for index, token in words:
        upper = token.upper
        if upper in FORBIDDEN_WORDS:
            raise _reject(f"{upper} is not allowed in a read-only statement")
        if kind == "EXPLAIN" and upper in EXPLAIN_FORBIDDEN:
            raise _reject("EXPLAIN ANALYZE executes the statement and is not allowed")
        if upper == "FOR" and _next_word(tokens, index) in LOCKING_AFTER_FOR:
            raise _reject("locking reads (FOR SHARE / FOR NO KEY UPDATE) are not allowed")


def _next_word(tokens: list[Token], index: int) -> str:
    following = tokens[index + 1] if index + 1 < len(tokens) else None
    return following.upper if following is not None and following.kind is Kind.WORD else ""


def _check_functions(tokens: list[Token]) -> None:
    for index, token in enumerate(tokens[:-1]):
        callable_name = token.kind in {Kind.WORD, Kind.QUOTED}
        following = tokens[index + 1]
        if callable_name and following.kind is Kind.PUNCT and following.text == "(":
            _check_function_name(token.text.lower())


def _check_function_name(name: str) -> None:
    if name.startswith(FORBIDDEN_FUNCTION_PREFIXES):
        raise _reject(f"function {name}() is not allowed in the console")
