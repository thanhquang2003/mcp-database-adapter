import pytest
from sqlglot.dialects.dialect import Dialect
from sqlglot.dialects.mysql import MySQL
from sqlglot.dialects.oracle import Oracle

from mcp_database.guard.parser import ParsedQuery, QueryRejected, parse_select


@pytest.fixture(params=["oracle", "mariadb"])
def db_type(request):
    return request.param


def rejection(sql: str, db_type: str) -> QueryRejected:
    with pytest.raises(QueryRejected) as info:
        parse_select(sql, db_type)
    return info.value


OK = [
    "SELECT a FROM t",
    "select a from t;",
    "WITH x AS (SELECT a FROM t) SELECT a FROM x",
    "SELECT a FROM t -- ; DROP TABLE t",
    "SELECT ';' AS s FROM t",
    "SELECT a FROM t /* ; */",
]


@pytest.mark.parametrize("sql", OK)
def test_accepts(sql, db_type):
    parsed = parse_select(sql, db_type)
    assert isinstance(parsed, ParsedQuery)
    assert parsed.tables[0] in ("T",)


BAD = [
    "SELECT 1; DROP TABLE t",
    "DELETE FROM t",
    "UPDATE t SET a=1",
    "INSERT INTO t VALUES (1)",
    "DROP TABLE t",
    "TRUNCATE TABLE t",
    "SELECT a FROM t FOR UPDATE",
    "SELECT a INTO x FROM t",
    "CREATE TABLE x AS SELECT 1 FROM dual",
    "BEGIN NULL; END;",
    "SELECT dbms_lock.sleep(5) FROM dual",
    "SELECT utl_http.request('http://x') FROM dual",
]


@pytest.mark.parametrize("sql", BAD)
def test_rejects(sql, db_type):
    err = rejection(sql, db_type)
    assert err.reason
    assert err.hint


# Đầu vào tấn công bổ sung ngoài danh sách của brief.
ADVERSARIAL = [
    "",
    "   \n\t ",
    "/* chỉ có comment */",
    "-- chỉ có comment",
    ";",
    "SELECT 'a;b' FROM t; DROP TABLE t",
    "SELECT a FROM t; SELECT b FROM u",
    "WITH x AS (DELETE FROM t RETURNING a) SELECT a FROM x",
    "WITH x AS (SELECT a FROM t) UPDATE t SET a = 1",
    "SELECT a FROM (SELECT a FROM t FOR UPDATE) s",
    "SELECT a FROM t WHERE a IN (SELECT a FROM u FOR UPDATE)",
    "SELECT Dbms_Lock.Sleep(5) FROM dual",
    'SELECT "DBMS_LOCK"."SLEEP"(5) FROM dual',
    "SELECT sys.dbms_lock.sleep(1) FROM dual",
    "SELECT dbms_session.unique_session_id FROM dual",
    "SELECT * FROM TABLE(dbms_xplan.display)",
    "SELECT app.pkg.do_something(1) FROM dual",
    "MERGE INTO t USING u ON (t.id = u.id) WHEN MATCHED THEN UPDATE SET t.a = u.a",
    "ALTER TABLE t ADD c INT",
    "GRANT SELECT ON t TO x",
    "CALL p()",
    "SET @a = 1",
]


@pytest.mark.parametrize("sql", ADVERSARIAL)
def test_rejects_adversarial(sql, db_type):
    assert rejection(sql, db_type).hint


@pytest.mark.parametrize(
    "sql",
    [
        "EXECUTE IMMEDIATE 'DROP TABLE t'",
        "SELECT a FROM t FOR UPDATE NOWAIT",
        "SELECT a FROM t FOR UPDATE OF a SKIP LOCKED",
        "LOCK TABLE t IN EXCLUSIVE MODE",
        "SELECT a FROM t@remote_link",
    ],
)
def test_oracle_adversarial(sql):
    assert rejection(sql, "oracle").hint


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT SLEEP(5)",
        "SELECT sleep (5) FROM t",
        "SELECT `sleep`(5) FROM t",
        "SELECT BENCHMARK(1000000, MD5('a'))",
        "SELECT LOAD_FILE('/etc/passwd')",
        "SELECT GET_LOCK('a', 10)",
        "SELECT RELEASE_LOCK('a')",
        "SELECT a FROM t INTO OUTFILE '/tmp/x'",
        "SELECT a FROM t INTO DUMPFILE '/tmp/x'",
        "SELECT a FROM t INTO @v",
        "SELECT a FROM t LOCK IN SHARE MODE",
        "SELECT a FROM t FOR SHARE",
        "SELECT @a := 1 FROM t",
    ],
)
def test_mariadb_blocked_functions(sql):
    assert rejection(sql, "mariadb").hint


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a /*!50000 , SLEEP(5) */ FROM t",
        "SELECT a FROM t /*M! ; DROP TABLE t */",
        "SELECT a FROM t /*m!100000 x */",
        "/*!50000 SELECT*/ 1",
        "SELECT a FROM t /*!*/",
    ],
)
def test_mariadb_executable_comment_rejected(sql):
    err = rejection(sql, "mariadb")
    assert "thực thi" in err.reason
    assert err.hint


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT '/*!50000 x */' AS s FROM t",
        "SELECT a FROM t /* ! không phải lệnh */",
        "SELECT a FROM t -- /*! chỉ là comment dòng */",
        "SELECT a FROM t # /*! chỉ là comment dòng */",
        "SELECT /*+ MAX_EXECUTION_TIME(1000) */ a FROM t",
    ],
)
def test_mariadb_ordinary_comments_and_strings_ok(sql):
    assert parse_select(sql, "mariadb").tables == ("T",)


def test_oracle_bang_comment_not_rejected():
    assert parse_select("SELECT a FROM t /*! ghi chú */", "oracle").tables == ("T",)


def test_oracle_hint_stripped():
    parsed = parse_select("SELECT /*+ PARALLEL(t 8) */ a FROM t", "oracle")
    assert "PARALLEL" not in parsed.sql.upper()
    assert "/*" not in parsed.sql
    assert parsed.tables == ("T",)


def test_comments_stripped_from_sql(db_type):
    parsed = parse_select("SELECT a -- x\nFROM t /* y */", db_type)
    assert "--" not in parsed.sql and "/*" not in parsed.sql


def test_trailing_semicolon_with_comment_accepted(db_type):
    assert parse_select("SELECT a FROM t; -- xong", db_type).tables == ("T",)


def test_dialect_recorded():
    assert issubclass(type(Dialect.get_or_raise(parse_select("SELECT a FROM t", "oracle").dialect)), Oracle)
    assert issubclass(type(Dialect.get_or_raise(parse_select("SELECT a FROM t", "mariadb").dialect)), MySQL)


# Round-trip: SQL sinh lại phải chạy được trên Oracle 12c/19c và MariaDB.
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a FROM t MINUS SELECT a FROM u",
        "select a from t minus select a from u",
        "WITH x AS (SELECT a FROM t MINUS SELECT a FROM u) SELECT a FROM x",
        "SELECT a FROM t WHERE a IN (SELECT a FROM t MINUS SELECT a FROM u)",
    ],
)
def test_oracle_minus_round_trips(sql):
    parsed = parse_select(sql, "oracle")
    for out in (parsed.sql, parsed.expression.sql(dialect=parsed.dialect)):
        assert "MINUS" in out.upper()
        assert "EXCEPT" not in out.upper()
    assert parse_select(parsed.sql, "oracle").sql == parsed.sql


def test_oracle_other_set_ops_unchanged():
    assert "UNION ALL" in parse_select("SELECT a FROM t UNION ALL SELECT a FROM u", "oracle").sql
    assert "INTERSECT" in parse_select("SELECT a FROM t INTERSECT SELECT a FROM u", "oracle").sql


@pytest.mark.parametrize(
    "sql",
    ["SELECT a FROM t MINUS ALL SELECT a FROM u", "SELECT a FROM t INTERSECT ALL SELECT a FROM u"],
)
def test_oracle_set_op_all_rejected(sql):
    assert rejection(sql, "oracle").hint


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("SELECT a FROM t WHERE a REGEXP 'x'", "A REGEXP 'X'"),
        ("SELECT a FROM t WHERE a RLIKE 'x'", "A REGEXP 'X'"),
        ("SELECT a FROM t WHERE a NOT REGEXP 'x'", "A NOT REGEXP 'X'"),
        ("SELECT a FROM t WHERE a NOT RLIKE 'x'", "A NOT REGEXP 'X'"),
        ("SELECT a FROM t WHERE (a REGEXP 'x') = 1 AND b NOT REGEXP 'y'", "B NOT REGEXP 'Y'"),
    ],
)
def test_mariadb_regexp_round_trips(sql, expected):
    parsed = parse_select(sql, "mariadb")
    for out in (parsed.sql, parsed.expression.sql(dialect=parsed.dialect)):
        assert expected in out.upper()
        assert "REGEXP_LIKE" not in out.upper()
    assert parse_select(parsed.sql, "mariadb").sql == parsed.sql


def test_oracle_regexp_like_kept():
    assert "REGEXP_LIKE(a, 'x', 'i')" in parse_select("SELECT a FROM t WHERE REGEXP_LIKE(a, 'x', 'i')", "oracle").sql


def test_mariadb_regexp_like_with_flag_rejected():
    assert rejection("SELECT a FROM t WHERE REGEXP_LIKE(a, 'x', 'i')", "mariadb").hint


@pytest.mark.parametrize(
    "sql, db",
    [
        ("SELECT f@remote_link(1) FROM dual", "oracle"),
        ("SELECT a FROM t WHERE a = f@remote_link(1)", "oracle"),
        ("SELECT s.NEXTVAL FROM dual", "oracle"),
        ("SELECT app.s.nextval FROM dual", "oracle"),
        ("SELECT s.nextval FROM dual", "mariadb"),
        ("SELECT NEXTVAL(s)", "mariadb"),
        ("SELECT nextval(s) FROM t", "mariadb"),
        ("SELECT SETVAL(s, 100)", "mariadb"),
        ("SELECT LASTVAL(s)", "mariadb"),
        ("SELECT MASTER_POS_WAIT('f', 1)", "mariadb"),
        ("SELECT MASTER_GTID_WAIT('0-1-1')", "mariadb"),
    ],
)
def test_hardened_rejections(sql, db):
    assert rejection(sql, db).hint


def test_currval_allowed():
    assert parse_select("SELECT s.CURRVAL FROM dual", "oracle").tables == ("DUAL",)


def test_cte_name_not_a_table():
    assert parse_select("WITH x AS (SELECT a FROM t) SELECT a FROM x", "oracle").tables == ("T",)


def test_schema_qualified_table_named_like_cte_is_a_table():
    parsed = parse_select("WITH x AS (SELECT a FROM t) SELECT a FROM app.x JOIN x ON 1 = 1", "oracle")
    assert set(parsed.tables) == {"T", "X"}


def test_tables_collected_from_subqueries_and_unions(db_type):
    parsed = parse_select(
        "SELECT a FROM t WHERE a IN (SELECT b FROM u) UNION ALL SELECT c FROM v JOIN t ON 1 = 1", db_type
    )
    assert parsed.tables == ("T", "U", "V")


def test_schema_and_quotes_normalized():
    assert parse_select('SELECT a FROM app."Cdr_History" c', "oracle").tables == ("CDR_HISTORY",)
    assert parse_select("SELECT a FROM app.`Cdr_History` c", "mariadb").tables == ("CDR_HISTORY",)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a FROM t CONNECT BY PRIOR a = b",
        "SELECT a FROM t, u WHERE t.id = u.id(+)",
        "WITH FUNCTION f RETURN NUMBER IS BEGIN RETURN 1; END; SELECT f FROM dual",
        "SELECT a FROM t\n/",
        "SELECT (((((",
    ],
)
def test_unparseable_is_rejection_not_crash(sql):
    try:
        parsed = parse_select(sql, "oracle")
    except QueryRejected as err:
        assert err.hint
    else:
        assert isinstance(parsed, ParsedQuery)


def test_oracle_specific_syntax_accepted():
    assert parse_select("SELECT a FROM t CONNECT BY PRIOR a = b", "oracle").tables == ("T",)
    assert parse_select("SELECT a FROM t, u WHERE t.id = u.id(+)", "oracle").tables == ("T", "U")


def test_parse_error_hint_mentions_standard_select():
    err = rejection("SELECT (((((", "oracle")
    assert "SELECT chuẩn" in err.hint


def test_rejection_has_hint():
    assert rejection("DELETE FROM t", "oracle").hint


def test_plain_functions_allowed(db_type):
    assert parse_select("SELECT COUNT(*), MAX(a), UPPER(b) FROM t GROUP BY c", db_type).tables == ("T",)


def test_unknown_db_type():
    with pytest.raises(ValueError):
        parse_select("SELECT 1 FROM t", "postgres")
