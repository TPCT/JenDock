"""The console statement guard (pure, no database)."""

from __future__ import annotations

import pytest

from backend.inspector.model import MAX_STATEMENT_CHARS, Engine, ErrorCode, InspectorError
from backend.inspector.sql_guard import validate_statement

PG = Engine.POSTGRESQL
MY = Engine.MYSQL
MARIA = Engine.MARIADB
ALL = [PG, MY, MARIA]


def rejected(sql: str, engine: Engine) -> str:
    with pytest.raises(InspectorError) as raised:
        validate_statement(sql, engine)
    assert raised.value.code is ErrorCode.REJECTED
    return raised.value.message


@pytest.mark.parametrize("engine", ALL)
@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        ("SELECT 1", "SELECT"),
        ("select * from t where a = 'x;y'", "SELECT"),
        ("  SELECT 1 ;  ", "SELECT"),
        ("SELECT 1; -- trailing comment", "SELECT"),
        ("(SELECT 1) UNION (SELECT 2)", "SELECT"),
        ("WITH a AS (SELECT 1) SELECT * FROM a", "WITH"),
        ("SHOW TABLES", "SHOW"),
        ("EXPLAIN SELECT * FROM t", "EXPLAIN"),
        ("SELECT 'DROP TABLE x' AS s", "SELECT"),
        ('SELECT "update" FROM t', "SELECT"),
        ("SELECT replace(a, 'x', 'y') FROM t", "SELECT"),
        ("SELECT /* DELETE */ 1", "SELECT"),
        ("SELECT 1 /* a ; b */", "SELECT"),
        ("SELECT 100 % 7", "SELECT"),
        ("SELECT x FROM t WHERE y LIKE '%a%'", "SELECT"),
    ],
)
def test_accepts_one_read_only_statement(sql: str, kind: str, engine: Engine) -> None:
    assert validate_statement(sql, engine).kind == kind


def test_trailing_semicolon_and_comment_are_removed() -> None:
    assert validate_statement("SELECT 1 ; -- bye", PG).text == "SELECT 1"
    assert validate_statement("  SELECT 1;", MY).text == "SELECT 1"


@pytest.mark.parametrize("engine", ALL)
@pytest.mark.parametrize(
    "sql",
    [
        "",
        "   ",
        "-- nothing",
        "/* nothing */",
        ";",
        "SELECT 1; SELECT 2",
        "SELECT 1;;",
        ";SELECT 1",
        "SELECT 1; DROP TABLE t",
        "INSERT INTO t VALUES (1)",
        "UPDATE t SET a = 1",
        "DELETE FROM t",
        "DROP TABLE t",
        "CREATE TABLE t (a int)",
        "ALTER TABLE t ADD b int",
        "TRUNCATE t",
        "GRANT ALL ON t TO u",
        "SET search_path = x",
        "BEGIN",
        "COMMIT",
        "CALL p()",
        "DO $$ BEGIN END $$",
        "COPY t TO '/tmp/x'",
        "REPLACE INTO t VALUES (1)",
        "VACUUM",
        "LOAD DATA INFILE '/x' INTO TABLE t",
        "SELECT * INTO OUTFILE '/tmp/x' FROM t",
        "SELECT * INTO DUMPFILE '/tmp/x' FROM t",
        "SELECT a INTO @v FROM t",
        "SELECT a INTO newtable FROM t",
        "SELECT * FROM t FOR UPDATE",
        "SELECT * FROM t FOR SHARE",
        "SELECT * FROM t FOR NO KEY UPDATE",
        "SELECT * FROM t LOCK IN SHARE MODE",
        "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d",
        "WITH i AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM i",
        "EXPLAIN ANALYZE SELECT * FROM t",
        "EXPLAIN (ANALYZE, BUFFERS) SELECT * FROM t",
        "EXPLAIN ANALYSE SELECT 1",
        "EXPLAIN DELETE FROM t",
        "SELECT pg_sleep(1)",
        "SELECT pg_catalog.pg_sleep(1)",
        "SELECT PG_SLEEP (1)",
        "SELECT pg_sleep /* x */ (1)",
        "SELECT sleep(1)",
        "SELECT benchmark(100000000, md5('a'))",
        "SELECT get_lock('a', 1)",
        "SELECT load_file('/etc/passwd')",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT pg_ls_dir('.')",
        "SELECT lo_import('/etc/passwd')",
        "SELECT set_config('default_transaction_read_only', 'off', false)",
        "SELECT nextval('s')",
        "SELECT setval('s', 1)",
        "SELECT pg_terminate_backend(1)",
        "SELECT pg_advisory_lock(1)",
        "SELECT dblink('x', 'DELETE FROM t')",
        "SELECT query_to_xml('DELETE FROM t', true, true, '')",
        "SELECT 'unterminated",
        "SELECT 1 /* unterminated",
        "SELECT\x00 1",
    ],
)
def test_rejects_anything_but_one_read_only_statement(sql: str, engine: Engine) -> None:
    rejected(sql, engine)


def test_statement_longer_than_the_limit_is_rejected() -> None:
    assert "longer" in rejected("SELECT '" + "a" * MAX_STATEMENT_CHARS + "'", PG)


def test_show_create_table_is_allowed_but_not_for_other_statements() -> None:
    assert validate_statement("SHOW CREATE TABLE t", MY).kind == "SHOW"
    rejected("SELECT 1 FROM (SELECT 1) CREATE", PG)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 -- ; DROP TABLE t",
        "SELECT 'a'; -- x",
    ],
)
def test_line_comments_hide_text_only_until_the_end_of_the_line(sql: str) -> None:
    validate_statement(sql, PG)


def test_comment_cannot_hide_a_second_statement_on_the_next_line() -> None:
    rejected("SELECT 1 -- hi\n; DROP TABLE t", PG)
    rejected("SELECT 1 -- hi\n; DROP TABLE t", MY)


# --- dialect specific lexing -----------------------------------------------------------


def test_mysql_double_dash_needs_whitespace_to_be_a_comment() -> None:
    # In MySQL "--x" is two minus signs, so the text after it is code the guard must see.
    rejected("SELECT 1 --x; DROP TABLE t", MY)
    rejected("SELECT 1 --x\n; DROP TABLE t", MY)
    validate_statement("SELECT 5 -- 1; DROP TABLE t", MY)


def test_mysql_hash_comment_and_postgres_hash_operator() -> None:
    validate_statement("SELECT 1 # ; DROP TABLE t", MY)
    rejected("SELECT 1 # x\n; DROP TABLE t", MY)
    # '#' is an operator in PostgreSQL, so what follows is code.
    rejected("SELECT 1 # ; DROP TABLE t", PG)


@pytest.mark.parametrize("engine", [MY, MARIA])
def test_mysql_executable_comments_are_rejected(engine: Engine) -> None:
    assert "executable" in rejected("SELECT 1 /*!50000 ; DROP TABLE t */", engine)
    rejected("SELECT 1 /*M!100100 ; DROP TABLE t */", engine)
    validate_statement("SELECT /*+ MAX_EXECUTION_TIME(10) */ 1", engine)


def test_mysql_backslash_escapes_keep_the_string_open() -> None:
    validate_statement(r"SELECT 'it\'s; fine' AS s", MY)
    rejected(r"SELECT 'a\'; DROP TABLE t; --'", PG)  # PostgreSQL: backslash is literal
    validate_statement(r"SELECT 'a\'' AS s", MY)


def test_postgres_standard_strings_and_escape_strings() -> None:
    validate_statement(r"SELECT 'a\' AS s", PG)
    rejected(r"SELECT 'a\'; DROP TABLE t; --'", PG)
    validate_statement(r"SELECT E'it\'s; fine' AS s", PG)


def test_postgres_dollar_quoting_hides_text_like_a_string() -> None:
    assert validate_statement("SELECT $$ ; DROP TABLE t $$", PG).kind == "SELECT"
    assert validate_statement("SELECT $tag$ ; DROP $$ TABLE t $tag$", PG).kind == "SELECT"
    rejected("SELECT $$ unterminated", PG)
    rejected("SELECT $a$ x $b$ ; DROP TABLE t", PG)
    # '$1' is a parameter, not a quote; in MySQL '$' is just a word character.
    validate_statement("SELECT $1", PG)


def test_postgres_nested_block_comments() -> None:
    validate_statement("SELECT 1 /* a /* b */ ; c */", PG)
    rejected("SELECT 1 /* a /* b */ */ ; DROP TABLE t", PG)


def test_mysql_block_comments_do_not_nest() -> None:
    rejected("SELECT 1 /* a /* b */ ; DROP TABLE t */", MY)


def test_postgres_unicode_escape_identifiers_are_rejected() -> None:
    rejected('SELECT U&"pg\\005fsleep"(1)', PG)
    rejected("SELECT u&'x'", PG)


def test_quoted_identifiers_are_not_keywords_but_are_checked_as_functions() -> None:
    validate_statement('SELECT "drop", "insert" FROM "update"', PG)
    validate_statement("SELECT `drop`, `into` FROM `update`", MY)
    rejected("SELECT `sleep`(1)", MY)
    rejected('SELECT s."pg_sleep"(1)', PG)


def test_mysql_double_quotes_are_strings() -> None:
    validate_statement('SELECT "a;b" AS s', MY)
    rejected('SELECT "a" ; DROP TABLE t', MY)
