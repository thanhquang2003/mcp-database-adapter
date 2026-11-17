import pytest

from mcp_database.guard.parser import QueryRejected, parse_select
from mcp_database.masking import apply_masking, plan_masking

S = {"CDR.MSISDN": "hash", "USERS.FULL_NAME": "redact"}


def P(sql):
    return parse_select(sql, "oracle")


def M(sql):
    return parse_select(sql, "mariadb")


# --- Từ brief -----------------------------------------------------------------


def test_bare_columns_mapped_by_position():
    assert plan_masking(
        P("SELECT c.msisdn AS m, status, COUNT(*) n FROM cdr c GROUP BY c.msisdn, status"), S
    ) == ["hash", None, None]


def test_allowed_in_where_join_order():
    assert plan_masking(P("SELECT status FROM cdr c WHERE c.msisdn = '8491' ORDER BY c.msisdn"), S) == []


LEAKS = [
    "SELECT * FROM cdr",
    "SELECT c.* FROM cdr c",
    "SELECT msisdn || 'x' FROM cdr",
    "SELECT MAX(msisdn) FROM cdr",
    "SELECT SUBSTR(msisdn,1,3) FROM cdr",
    "SELECT CASE WHEN msisdn IS NULL THEN 1 ELSE 0 END FROM cdr",
    "WITH x AS (SELECT msisdn FROM cdr) SELECT msisdn FROM x",
    "SELECT m FROM (SELECT msisdn AS m FROM cdr)",
    "SELECT msisdn FROM cdr UNION SELECT msisdn FROM cdr",
]


@pytest.mark.parametrize("sql", LEAKS)
def test_leaks_rejected(sql):
    with pytest.raises(QueryRejected):
        plan_masking(P(sql), S)


def test_no_sensitive_tables_no_restriction():
    assert plan_masking(P("SELECT * FROM other"), S) == []


def test_apply_masking():
    out = apply_masking([("84912", "a", None)], ["hash", None, "hash"], b"k")
    assert out[0][1] == "a" and out[0][2] is None and len(out[0][0]) == 16 and out[0][0] != "84912"
    assert apply_masking([("84912",)], ["hash"], b"k") == apply_masking([("84912",)], ["hash"], b"k")
    assert apply_masking([("84912",)], ["hash"], b"k") != apply_masking([("84912",)], ["hash"], b"k2")
    assert apply_masking([("Nguyen",)], ["redact"], b"k") == [("***",)]


# --- Bổ sung: những gì phải được phép ----------------------------------------


@pytest.mark.parametrize(
    "sql, expected",
    [
        ("SELECT DISTINCT msisdn FROM cdr", ["hash"]),
        ('SELECT msisdn AS "Phone" FROM cdr', ["hash"]),
        ('SELECT C."MSISDN" FROM APP.CDR C', ["hash"]),
        ('SELECT "C"."MsIsDn" FROM "APP"."CDR" "C"', ["hash"]),
        ("SELECT c.msisdn FROM app.cdr c", ["hash"]),
        ("SELECT app.cdr.msisdn FROM app.cdr", ["hash"]),
        ("SELECT cdr.msisdn FROM cdr", ["hash"]),
        ("SELECT u.full_name, c.msisdn FROM cdr c JOIN users u ON u.id = c.user_id", ["redact", "hash"]),
        ("SELECT full_name, status FROM users, cdr", ["redact", None]),
        # cùng tên cột ở bảng không nhạy cảm, có qualifier rõ ràng → không che
        ("SELECT o.msisdn FROM other o JOIN cdr c ON c.id = o.id", []),
        # CTE không chứa cột nhạy cảm, projection ngoài cùng lấy từ bảng thật
        ("WITH x AS (SELECT id FROM other) SELECT c.msisdn FROM cdr c JOIN x ON x.id = c.id", ["hash"]),
        ("(SELECT msisdn FROM cdr)", ["hash"]),
    ],
)
def test_allowed_projection(sql, expected):
    assert plan_masking(P(sql), S) == expected


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT status FROM cdr ORDER BY msisdn",
        "SELECT status, COUNT(*) FROM cdr GROUP BY status HAVING COUNT(DISTINCT msisdn) > 1",
        "SELECT COUNT(*) FROM cdr WHERE SUBSTR(msisdn, 1, 3) = '849'",
        "SELECT COUNT(*) OVER (PARTITION BY msisdn ORDER BY msisdn) FROM cdr",
        "SELECT u.id FROM users u JOIN cdr c ON c.msisdn = u.phone",
        "SELECT o.id FROM other o JOIN cdr c USING (msisdn)",
        "SELECT id FROM users WHERE id IN (SELECT user_id FROM cdr WHERE msisdn = '8491')",
        "SELECT COUNT(*) FROM cdr",
        "SELECT (SELECT COUNT(*) FROM cdr x WHERE x.msisdn = c.msisdn) FROM cdr c",
        "SELECT status FROM cdr UNION SELECT status FROM cdr WHERE msisdn = '1'",
    ],
)
def test_allowed_without_projecting(sql):
    assert plan_masking(P(sql), S) == []


def test_cte_shadowing_sensitive_table_name():
    # CDR ở đây là CTE, không phải bảng thật → không có gì nhạy cảm
    assert plan_masking(P("WITH cdr AS (SELECT phone AS msisdn FROM other) SELECT msisdn FROM cdr"), S) == []


def test_mariadb_backticks():
    assert plan_masking(M("SELECT `c`.`MSISDN` FROM `app`.`cdr` `c`"), S) == ["hash"]
    with pytest.raises(QueryRejected):
        plan_masking(M("SELECT CONCAT(msisdn, '') FROM cdr"), S)


# --- Bổ sung: đường rò phải bị chặn -------------------------------------------


MORE_LEAKS = [
    "SELECT id FROM users WHERE phone IN (SELECT msisdn FROM cdr)",
    "SELECT (SELECT msisdn FROM cdr WHERE ROWNUM = 1) AS p FROM dual",
    "SELECT * FROM other WHERE id IN (SELECT user_id FROM cdr)",
    "SELECT o.* FROM other o JOIN cdr c ON c.id = o.id",
    "SELECT m.msisdn FROM (SELECT * FROM cdr) m",
    "SELECT LAG(msisdn) OVER (ORDER BY id) FROM cdr",
    "SELECT msisdn + 0 FROM cdr",
    "SELECT (msisdn) FROM cdr",
    "SELECT CAST(msisdn AS VARCHAR2(20)) FROM cdr",
    "SELECT UPPER(full_name) FROM users",
    "SELECT LISTAGG(msisdn, ',') WITHIN GROUP (ORDER BY msisdn) FROM cdr",
    "SELECT status FROM cdr UNION SELECT msisdn FROM cdr",
    "SELECT status FROM cdr MINUS SELECT msisdn FROM cdr",
    "SELECT status FROM cdr INTERSECT SELECT msisdn FROM cdr",
    "SELECT val FROM cdr UNPIVOT (val FOR col IN (msisdn, status))",
    "SELECT x FROM cdr PIVOT (MAX(msisdn) FOR status IN (1 AS x))",
    "SELECT x FROM other JOIN cdr PIVOT (MAX(msisdn) FOR status IN (1 AS x)) ON 1 = 1",
    "SELECT EXISTS_X FROM (SELECT msisdn AS EXISTS_X FROM cdr)",
    "WITH a AS (SELECT c.msisdn AS k FROM cdr c) SELECT k FROM a",
    # qualifier không rõ nguồn gốc → coi như có thể là bảng nhạy cảm
    "SELECT SUBSTR(q.msisdn, 1, 3) FROM cdr c, other o",
    # cột không qualifier trùng tên khi có bảng nhạy cảm trong query
    "SELECT MAX(msisdn) FROM other o JOIN cdr c ON c.id = o.id",
    "SELECT msisdn || '' FROM cdr ORDER BY msisdn",
]


@pytest.mark.parametrize("sql", MORE_LEAKS)
def test_more_leaks_rejected(sql):
    with pytest.raises(QueryRejected) as info:
        plan_masking(P(sql), S)
    assert info.value.hint  # luôn kèm gợi ý tiếng Việt


def test_rejection_message_is_vietnamese_with_hint():
    with pytest.raises(QueryRejected) as info:
        plan_masking(P("SELECT MAX(msisdn) FROM cdr"), S)
    assert "CDR.MSISDN" in info.value.reason
    assert "nhạy cảm" in info.value.reason
    assert info.value.hint


def test_star_rejection_names_reason():
    with pytest.raises(QueryRejected) as info:
        plan_masking(P("SELECT * FROM cdr"), S)
    assert "*" in info.value.reason


# --- apply_masking bổ sung -----------------------------------------------------


def test_apply_masking_empty_plan_returns_rows():
    rows = [(1, "a"), (2, None)]
    assert apply_masking(rows, [], b"k") is rows


def test_apply_masking_non_string_values():
    out = apply_masking([(84912, None, 3.5)], ["hash", "redact", "redact"], b"k")
    assert out == [(apply_masking([("84912",)], ["hash"], b"k")[0][0], None, "***")]


def test_apply_masking_known_hmac():
    import hashlib
    import hmac

    expected = hmac.new(b"k", b"84912", hashlib.sha256).hexdigest()[:16]
    assert apply_masking([("84912",)], ["hash"], b"k") == [(expected,)]


# --- Round 1 review fixes -----------------------------------------------------

XQUERY_BYPASS = [
    """SELECT COLUMN_VALUE FROM XMLTABLE('fn:collection("oradb:/APP/CDR")')""",
    """SELECT x.column_value FROM XMLTABLE('ora:view("CDR")') x""",
    """SELECT x.* FROM XMLTABLE('fn:collection("oradb:/APP/CDR")') x""",
]


@pytest.mark.parametrize("sql", XQUERY_BYPASS)
def test_xquery_rejected_by_parser(sql):
    with pytest.raises(QueryRejected):
        P(sql)


def _unguarded(sql, dialect="mcporacle"):
    """ParsedQuery dựng thẳng từ sqlglot, bỏ qua parse_select, để thử lớp phòng thủ thứ hai."""
    import sqlglot

    from mcp_database.guard.parser import ParsedQuery, _real_tables

    tree = sqlglot.parse_one(sql, read=dialect)
    return ParsedQuery(expression=tree, dialect=dialect, sql=sql, tables=_real_tables(tree))


@pytest.mark.parametrize("sql", XQUERY_BYPASS)
def test_xquery_rejected_by_masking_even_without_parser(sql):
    with pytest.raises(QueryRejected) as info:
        plan_masking(_unguarded(sql), S)
    assert info.value.hint


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a FROM TABLE(f(x))",
        "SELECT j.a FROM JSON_TABLE(x, '$' COLUMNS (a NUMBER PATH '$.a')) j",
        "SELECT o.id FROM other o JOIN TABLE(f(o.x)) t ON 1 = 1",
    ],
)
def test_table_function_sources_rejected_when_sensitive_configured(sql):
    with pytest.raises(QueryRejected):
        plan_masking(P(sql), S)
    assert plan_masking(P(sql), {}) == []


def test_lateral_subquery_is_a_plain_source():
    assert plan_masking(P("SELECT o.id FROM other o CROSS APPLY (SELECT 1 AS k FROM dual) z"), S) == []


ROW_GENERATORS_ON_SENSITIVE = [
    ("SELECT n.v FROM (SELECT LEVEL + 84900000000 v FROM dual CONNECT BY LEVEL <= 1000000) n "
     "JOIN cdr c ON c.msisdn = TO_CHAR(n.v)", "oracle"),
    ("SELECT status FROM cdr START WITH id = 1 CONNECT BY PRIOR id = parent_id", "oracle"),
    ("WITH RECURSIVE r AS (SELECT 1 AS v FROM dual UNION ALL SELECT v + 1 FROM r WHERE v < 9) "
     "SELECT r.v FROM r JOIN cdr c ON c.msisdn = r.v", "oracle"),
    ("WITH RECURSIVE r AS (SELECT 1 AS v UNION ALL SELECT v + 1 FROM r WHERE v < 9) "
     "SELECT r.v FROM r JOIN cdr c ON c.msisdn = r.v", "mariadb"),
    ("WITH r (v) AS (SELECT 1 FROM dual UNION ALL SELECT v + 1 FROM r WHERE v < 9) "
     "SELECT r.v FROM r JOIN cdr c ON c.msisdn = r.v", "oracle"),
    ("SELECT s.seq FROM seq_84900000000_to_84999999999 s JOIN cdr c ON c.msisdn = s.seq", "mariadb"),
    ("SELECT s.seq FROM seq_1_to_100_step_2 s JOIN cdr c ON c.msisdn = s.seq", "mariadb"),
]


@pytest.mark.parametrize("sql, db", ROW_GENERATORS_ON_SENSITIVE)
def test_row_generators_rejected_with_sensitive_table(sql, db):
    with pytest.raises(QueryRejected) as info:
        plan_masking(parse_select(sql, db), S)
    assert info.value.hint


@pytest.mark.parametrize(
    "sql, db",
    [
        ("SELECT LEVEL FROM dual CONNECT BY LEVEL <= 10", "oracle"),
        ("SELECT id FROM other START WITH id = 1 CONNECT BY PRIOR id = parent_id", "oracle"),
        ("WITH RECURSIVE r AS (SELECT 1 AS v UNION ALL SELECT v + 1 FROM r WHERE v < 9) SELECT v FROM r", "mariadb"),
        ("WITH r (v) AS (SELECT 1 FROM dual UNION ALL SELECT v + 1 FROM r WHERE v < 9) SELECT v FROM r", "oracle"),
        ("SELECT seq FROM seq_1_to_10", "mariadb"),
    ],
)
def test_row_generators_allowed_without_sensitive_table(sql, db):
    assert plan_masking(parse_select(sql, db), S) == []


def test_keep_dense_rank_order_is_allowed():
    assert plan_masking(P("SELECT MAX(id) KEEP (DENSE_RANK FIRST ORDER BY msisdn) FROM cdr"), S) == []


def test_numeric_values_hash_identically():
    from decimal import Decimal

    expected = apply_masking([("84912",)], ["hash"], b"k")[0][0]
    for v in (84912, 84912.0, Decimal("84912"), Decimal("84912.00")):
        assert apply_masking([(v,)], ["hash"], b"k")[0][0] == expected, v
    assert apply_masking([(Decimal("1.5"),)], ["hash"], b"k") == apply_masking([("1.5",)], ["hash"], b"k")
    assert apply_masking([(True,)], ["hash"], b"k") == apply_masking([("True",)], ["hash"], b"k")
