import pytest

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


def test_mariadb_executable_comment_not_in_output():
    parsed = parse_select("SELECT a /*!50000 , SLEEP(5) */ FROM t /*M! ; DROP TABLE t */", "mariadb")
    assert "SLEEP" not in parsed.sql.upper()
    assert "DROP" not in parsed.sql.upper()
    assert "/*" not in parsed.sql


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
    assert parse_select("SELECT a FROM t", "oracle").dialect == "oracle"
    assert parse_select("SELECT a FROM t", "mariadb").dialect == "mysql"


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
