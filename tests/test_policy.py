from datetime import date
from types import MappingProxyType

import pytest
from sqlglot import exp

from mcp_database.config import Guard, TableGuard
from mcp_database.guard.parser import QueryRejected, parse_select
from mcp_database.guard.policy import apply_row_limit, enforce_time_range, large_tables

G = Guard(tables=MappingProxyType({"CDR_HISTORY": TableGuard("CALL_TIME", 31)}))
TODAY = date(2026, 11, 17)


def P(sql):
    return parse_select(sql, "oracle")


def PM(sql):
    return parse_select(sql, "mariadb")


def E(sql, large=frozenset({"CDR_HISTORY"}), parse=P):
    enforce_time_range(parse(sql), G, set(large), TODAY)


def rejected(sql, parse=P, large=frozenset({"CDR_HISTORY"})) -> QueryRejected:
    with pytest.raises(QueryRejected) as info:
        E(sql, large, parse)
    return info.value


# --- Từ brief ---------------------------------------------------------------

GOOD = [
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= DATE '2026-11-01' AND call_time < DATE '2026-11-10'",
    "SELECT COUNT(*) FROM cdr_history c WHERE c.call_time BETWEEN TO_DATE('20261101','YYYYMMDD') AND TO_DATE('20261110','YYYYMMDD')",
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= SYSDATE - 7",
    "SELECT COUNT(*) FROM app.\"CDR_HISTORY\" WHERE \"CALL_TIME\" >= '2026-11-10' AND status = 1",
    "WITH x AS (SELECT * FROM cdr_history WHERE call_time >= DATE '2026-11-10') SELECT COUNT(*) FROM x",
]
BAD = [
    "SELECT COUNT(*) FROM cdr_history",
    "SELECT COUNT(*) FROM cdr_history WHERE status = 1",
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= DATE '2026-11-10' OR 1 = 1",
    "SELECT COUNT(*) FROM cdr_history WHERE TRUNC(call_time) >= DATE '2026-11-10'",
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= DATE '2026-01-01' AND call_time < DATE '2026-11-01'",
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= DATE '2026-01-01'",
    "SELECT COUNT(*) FROM cdr_history WHERE call_time < DATE '2026-11-10'",
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= some_col_expr(1)",
    "SELECT COUNT(*) FROM cdr_history c JOIN users u ON u.id = c.uid",
]


@pytest.mark.parametrize("sql", GOOD)
def test_good(sql):
    E(sql)


@pytest.mark.parametrize("sql", BAD)
def test_bad(sql):
    err = rejected(sql)
    assert err.hint


def test_other_table_unrestricted():
    enforce_time_range(P("SELECT COUNT(*) FROM users"), G, set(), TODAY)


def test_large_table_without_time_column_config():
    err = rejected("SELECT COUNT(*) FROM big WHERE created >= SYSDATE - 1", large={"BIG"})
    assert "khai báo guard.tables.BIG.time_column" in err.hint


def test_large_tables_detection():
    assert large_tables(P("SELECT 1 FROM a JOIN cdr_history b ON 1=1"), G, {"A": 10, "CDR_HISTORY": None}) == {"CDR_HISTORY"}
    assert large_tables(P("SELECT 1 FROM a"), G, {"A": 9_000_000}) == {"A"}
    assert large_tables(P("SELECT 1 FROM a"), G, {"A": None}) == set()


def test_large_tables_threshold_and_missing_stats():
    assert large_tables(P("SELECT 1 FROM a"), G, {"A": 5_000_000}) == {"A"}
    assert large_tables(P("SELECT 1 FROM a"), G, {"A": 4_999_999}) == set()
    assert large_tables(P("SELECT 1 FROM a"), G, {}) == set()
    # Bảng khai báo trong guard.tables luôn là bảng lớn, kể cả qua schema/nháy.
    assert large_tables(P('SELECT 1 FROM app."CDR_HISTORY"'), G, {}) == {"CDR_HISTORY"}


def test_limit_oracle():
    assert "FETCH FIRST 1000 ROWS ONLY" in apply_row_limit(P("SELECT a FROM t"), 1000).upper()


def test_limit_mariadb():
    assert apply_row_limit(PM("SELECT a FROM t"), 1000).upper().endswith("LIMIT 1000")


def test_limit_min_of_existing():
    assert "50" in apply_row_limit(PM("SELECT a FROM t LIMIT 50"), 1000)


def test_limit_cap_existing_larger():
    assert "1000" in apply_row_limit(PM("SELECT a FROM t LIMIT 99999"), 1000)


# --- Biến thể tên bảng / cột -------------------------------------------------

GOOD_NAMES = [
    "SELECT COUNT(*) FROM APP.CDR_HISTORY WHERE CALL_TIME >= SYSDATE - 7",
    'SELECT COUNT(*) FROM "CDR_HISTORY" h WHERE h."CALL_TIME" >= SYSDATE - 7',
    "SELECT COUNT(*) FROM app.cdr_history WHERE cdr_history.call_time >= SYSDATE - 7",
    "SELECT COUNT(*) FROM cdr_history c JOIN users u ON u.id = c.uid WHERE c.call_time >= SYSDATE - 7",
]


@pytest.mark.parametrize("sql", GOOD_NAMES)
def test_name_variants_accepted_with_filter(sql):
    E(sql)


BAD_NAMES = [
    "SELECT COUNT(*) FROM app.cdr_history",
    'SELECT COUNT(*) FROM "CDR_HISTORY"',
    "SELECT COUNT(*) FROM cdr_history c",
    # Có alias thì chỉ alias mới trỏ tới bảng.
    "SELECT COUNT(*) FROM cdr_history c JOIN users u ON 1 = 1 WHERE u.call_time >= SYSDATE - 7",
    # Tên bảng gốc bị alias của bảng khác chiếm.
    "SELECT COUNT(*) FROM cdr_history x JOIN users cdr_history ON 1 = 1 WHERE cdr_history.call_time >= SYSDATE - 7",
    # Cột không qualifier khi Select có nhiều nguồn: không xác định được thuộc bảng nào.
    "SELECT COUNT(*) FROM cdr_history c JOIN users u ON u.id = c.uid WHERE call_time >= SYSDATE - 7",
    # Cùng một bảng lớn hai lần, chỉ một tham chiếu có cận.
    "SELECT COUNT(*) FROM cdr_history a JOIN cdr_history b ON a.id = b.id WHERE a.call_time >= SYSDATE - 7",
    # Bảng trong ngoặc ở FROM.
    "SELECT COUNT(*) FROM (cdr_history) WHERE call_time >= SYSDATE - 7",
]


@pytest.mark.parametrize("sql", BAD_NAMES)
def test_name_variants_rejected(sql):
    rejected(sql)


def test_same_table_twice_both_bounded():
    E(
        "SELECT COUNT(*) FROM cdr_history a JOIN cdr_history b ON a.id = b.id "
        "WHERE a.call_time >= SYSDATE - 7 AND b.call_time >= SYSDATE - 3"
    )


# --- Điều kiện thời gian giả / sai ------------------------------------------

BAD_FILTERS = [
    "SELECT COUNT(*) FROM cdr_history WHERE (call_time >= SYSDATE - 7 OR status = 1)",
    "SELECT COUNT(*) FROM cdr_history WHERE NOT (call_time < SYSDATE - 7)",
    "SELECT COUNT(*) FROM cdr_history WHERE call_time NOT BETWEEN SYSDATE - 7 AND SYSDATE",
    "SELECT COUNT(*) FROM cdr_history WHERE call_time + 0 >= SYSDATE - 7",
    "SELECT COUNT(*) FROM cdr_history WHERE other_time >= SYSDATE - 7",
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= call_time - 1",
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= (SELECT MAX(call_time) - 7 FROM cdr_history)",
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= SYSDATE - 32",
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= SYSDATE - INTERVAL '32' DAY",
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= SYSDATE - 7.5",
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= SYSDATE - -400",
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= SYSDATE - 99999999",
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= SYSDATE - INTERVAL '7' MONTH",
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= DATE '2026-11-01' + 7",
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= '01/11/2026'",
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= TO_DATE('01/11/2026','DD/MM/YYYY')",
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= '2026-13-01'",
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= 20261101",
    # Một cận hợp lệ, một cận không tính được: từ chối.
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= SYSDATE - 7 AND call_time >= some_fn(1)",
    # Cận thời gian chỉ nằm trong ON của JOIN, không phải WHERE.
    "SELECT COUNT(*) FROM cdr_history c JOIN users u ON u.id = c.uid AND c.call_time >= SYSDATE - 7",
    # Cận dưới quá xa với cận trên hợp lệ nằm ở trước.
    "SELECT COUNT(*) FROM cdr_history WHERE call_time < DATE '2026-11-10' AND call_time >= DATE '2025-11-01'",
    # Khoảng 32 ngày theo timestamp.
    "SELECT COUNT(*) FROM cdr_history WHERE call_time BETWEEN DATE '2026-10-01' AND TIMESTAMP '2026-11-01 00:00:01'",
    # CONNECT BY duyệt cả bảng trước khi lọc WHERE.
    "SELECT * FROM cdr_history WHERE call_time >= SYSDATE - 7 CONNECT BY PRIOR id = parent_id",
]


@pytest.mark.parametrize("sql", BAD_FILTERS)
def test_fake_or_invalid_filters_rejected(sql):
    rejected(sql)


GOOD_FILTERS = [
    # Cận viết ở vế phải.
    "SELECT COUNT(*) FROM cdr_history WHERE DATE '2026-11-01' <= call_time AND DATE '2026-11-10' > call_time",
    # Bằng nhau: một ngày.
    "SELECT COUNT(*) FROM cdr_history WHERE call_time = DATE '2026-11-01'",
    # > và < đều được.
    "SELECT COUNT(*) FROM cdr_history WHERE call_time > TIMESTAMP '2026-11-01 10:00:00' AND call_time <= '2026-12-01 10:00:00'",
    # Nhiều cận cùng cột: lấy giao (chặt nhất), cận nào cũng phải tính được.
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= DATE '2025-01-01' AND call_time >= DATE '2026-11-01' AND call_time < DATE '2026-11-20'",
    # AND lồng trong ngoặc vẫn là AND cấp cao nhất.
    "SELECT COUNT(*) FROM cdr_history WHERE (call_time >= SYSDATE - 7 AND status = 1)",
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= TRUNC(SYSDATE) - 30",
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= CURRENT_DATE - INTERVAL '31' DAY",
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= TO_DATE('2026-11-01 10:00:00','YYYY-MM-DD HH24:MI:SS')",
    "SELECT COUNT(*) FROM cdr_history WHERE call_time BETWEEN TO_DATE('2026-10-17','YYYY-MM-DD') AND SYSDATE",
    # Cận dưới ở tương lai: khoảng âm, không quét gì.
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= DATE '2026-12-01'",
]


@pytest.mark.parametrize("sql", GOOD_FILTERS)
def test_valid_filters_accepted(sql):
    E(sql)


GOOD_MARIADB = [
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= NOW() - INTERVAL 7 DAY",
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= CURDATE() - INTERVAL 31 DAY",
    "SELECT COUNT(*) FROM `app`.`cdr_history` c WHERE c.`call_time` >= DATE '2026-11-01' AND c.call_time < TIMESTAMP '2026-11-10 00:00:00'",
]
BAD_MARIADB = [
    # MariaDB: NOW() - 7 là phép trừ số, không phải trừ ngày.
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= NOW() - 7",
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= NOW() - INTERVAL 40 DAY",
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= DATE_SUB(NOW(), INTERVAL 7 DAY)",
    "SELECT COUNT(*) FROM `cdr_history`",
]


@pytest.mark.parametrize("sql", GOOD_MARIADB)
def test_mariadb_good(sql):
    E(sql, parse=PM)


@pytest.mark.parametrize("sql", BAD_MARIADB)
def test_mariadb_bad(sql):
    rejected(sql, parse=PM)


# --- Phạm vi Select: subquery, CTE, UNION ----------------------------------

BAD_SCOPES = [
    "SELECT COUNT(*) FROM (SELECT * FROM cdr_history) x WHERE x.call_time >= SYSDATE - 7",
    "WITH x AS (SELECT * FROM cdr_history) SELECT COUNT(*) FROM x WHERE call_time >= SYSDATE - 7",
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= SYSDATE - 7 UNION ALL SELECT COUNT(*) FROM cdr_history",
    "SELECT COUNT(*) FROM users u WHERE EXISTS (SELECT 1 FROM cdr_history c WHERE c.uid = u.id)",
    # Cận ở Select ngoài không bảo vệ Select con.
    "SELECT COUNT(*) FROM cdr_history c WHERE c.call_time >= SYSDATE - 7 AND c.uid IN (SELECT uid FROM cdr_history)",
    "SELECT (SELECT COUNT(*) FROM cdr_history) FROM dual",
]


@pytest.mark.parametrize("sql", BAD_SCOPES)
def test_scopes_rejected(sql):
    rejected(sql)


GOOD_SCOPES = [
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= SYSDATE - 7 UNION ALL SELECT COUNT(*) FROM cdr_history WHERE call_time >= SYSDATE - 1",
    "SELECT COUNT(*) FROM (SELECT * FROM cdr_history WHERE call_time >= SYSDATE - 7) x",
    # CTE trùng tên bảng lớn: tham chiếu là CTE, thân CTE đã có cận.
    "WITH cdr_history AS (SELECT * FROM app.cdr_history WHERE call_time >= SYSDATE - 7) SELECT COUNT(*) FROM cdr_history",
]


@pytest.mark.parametrize("sql", GOOD_SCOPES)
def test_scopes_accepted(sql):
    E(sql)


def test_rejection_messages_in_vietnamese_with_hint():
    err = rejected("SELECT COUNT(*) FROM cdr_history WHERE call_time >= some_fn(1)")
    assert "dùng literal ngày hoặc SYSDATE/NOW() ± n ngày" in err.hint
    err = rejected("SELECT COUNT(*) FROM cdr_history WHERE call_time >= DATE '2026-01-01'")
    assert "31" in str(err)


# --- Giới hạn dòng -----------------------------------------------------------


def test_limit_oracle_min_of_existing_fetch():
    out = apply_row_limit(P("SELECT a FROM t FETCH FIRST 50 ROWS ONLY"), 1000).upper()
    assert "FETCH FIRST 50 ROWS ONLY" in out and "1000" not in out


def test_limit_oracle_caps_existing_fetch():
    out = apply_row_limit(P("SELECT a FROM t FETCH FIRST 99999 ROWS ONLY"), 1000).upper()
    assert "FETCH FIRST 1000 ROWS ONLY" in out and "99999" not in out


def test_limit_mariadb_exact():
    assert apply_row_limit(PM("SELECT a FROM t LIMIT 50"), 1000) == "SELECT a FROM t LIMIT 50"
    assert apply_row_limit(PM("SELECT a FROM t LIMIT 99999"), 1000) == "SELECT a FROM t LIMIT 1000"


def test_limit_keeps_offset():
    assert apply_row_limit(PM("SELECT a FROM t LIMIT 5, 99999"), 1000) == "SELECT a FROM t LIMIT 1000 OFFSET 5"
    out = apply_row_limit(P("SELECT a FROM t ORDER BY a OFFSET 5 ROWS FETCH NEXT 50 ROWS ONLY"), 1000)
    assert out == "SELECT a FROM t ORDER BY a OFFSET 5 ROWS FETCH FIRST 50 ROWS ONLY"


def test_limit_union_applies_to_whole_statement():
    assert (
        apply_row_limit(P("SELECT a FROM t UNION SELECT a FROM u"), 1000)
        == "SELECT a FROM t UNION SELECT a FROM u FETCH FIRST 1000 ROWS ONLY"
    )
    assert (
        apply_row_limit(PM("SELECT a FROM t UNION SELECT a FROM u LIMIT 20"), 1000)
        == "SELECT a FROM t UNION SELECT a FROM u LIMIT 20"
    )
    # Kết quả parse lại được và giới hạn nằm ở gốc (câu UNION), không ở nhánh cuối.
    for parse in (P, PM):
        out = apply_row_limit(parse("SELECT a FROM t UNION SELECT a FROM u"), 1000)
        root = parse(out).expression
        assert root.args.get("limit") is not None
        assert root.expression.args.get("limit") is None


def test_limit_does_not_mutate_parsed():
    parsed = PM("SELECT a FROM t")
    apply_row_limit(parsed, 10)
    assert parsed.expression.args.get("limit") is None


def test_limit_output_is_valid_for_dialect():
    for parse in (P, PM):
        out = apply_row_limit(parse("WITH x AS (SELECT a FROM t) SELECT a FROM x ORDER BY a"), 7)
        assert parse(out).tables == ("T",)


def test_rejection_quotes_bound_in_target_dialect():
    err = rejected("SELECT COUNT(*) FROM cdr_history WHERE call_time >= SYSDATE - 7.5")
    assert "SYSDATE - 7.5" in err.reason


# --- Review round 1 ----------------------------------------------------------

TABLE_OPERATORS = [
    (P, "SELECT * FROM cdr_history MATCH_RECOGNIZE (ORDER BY id MEASURES 1 AS x ALL ROWS PER MATCH "
        "PATTERN (a) DEFINE a AS 1=1) WHERE call_time >= SYSDATE - 7"),
    (P, "SELECT * FROM cdr_history c MATCH_RECOGNIZE (ORDER BY id MEASURES 1 AS x ALL ROWS PER MATCH "
        "PATTERN (a) DEFINE a AS 1=1) WHERE call_time >= SYSDATE - 7"),
    (P, "SELECT * FROM cdr_history UNPIVOT (v FOR k IN (a, b)) WHERE call_time >= SYSDATE - 7"),
    (P, "SELECT * FROM cdr_history PIVOT (COUNT(*) FOR status IN (1,2)) WHERE call_time >= SYSDATE - 7"),
    (PM, "SELECT * FROM cdr_history UNPIVOT (v FOR k IN (a, b)) WHERE call_time >= NOW() - INTERVAL 7 DAY"),
    (PM, "SELECT * FROM cdr_history PIVOT (COUNT(*) FOR status IN (1,2)) WHERE call_time >= NOW() - INTERVAL 7 DAY"),
]


@pytest.mark.parametrize("parse,sql", TABLE_OPERATORS)
def test_table_operators_on_large_table_rejected(parse, sql):
    err = rejected(sql, parse=parse)
    if parse is PM:
        # MariaDB không có PIVOT/UNPIVOT: bị cổng parser từ chối trước (fix wave A).
        assert "không hỗ trợ" in err.reason
    else:
        assert "subquery/CTE" in err.hint


TABLE_CLAUSES_OK = [
    (P, "SELECT * FROM cdr_history c WHERE c.call_time >= SYSDATE - 7"),
    (P, "SELECT * FROM cdr_history PARTITION (p1) WHERE call_time >= SYSDATE - 7"),
    (P, "SELECT * FROM cdr_history AS OF TIMESTAMP SYSDATE - 1 WHERE call_time >= SYSDATE - 7"),
    (P, "SELECT * FROM cdr_history c SAMPLE (1) WHERE call_time >= SYSDATE - 7"),
    (PM, "SELECT * FROM cdr_history FORCE INDEX (i) WHERE call_time >= NOW() - INTERVAL 7 DAY"),
    # Lọc trước trong CTE rồi mới PIVOT/MATCH_RECOGNIZE trên kết quả.
    (P, "WITH x AS (SELECT * FROM cdr_history WHERE call_time >= SYSDATE - 7) "
        "SELECT * FROM x PIVOT (COUNT(*) FOR status IN (1,2))"),
    (P, "WITH x AS (SELECT * FROM cdr_history WHERE call_time >= SYSDATE - 7) SELECT * FROM x "
        "MATCH_RECOGNIZE (ORDER BY id MEASURES 1 AS m ALL ROWS PER MATCH PATTERN (a) DEFINE a AS 1=1)"),
]


@pytest.mark.parametrize("parse,sql", TABLE_CLAUSES_OK)
def test_plain_table_clauses_and_prefiltered_cte_accepted(parse, sql):
    E(sql, parse=parse)


@pytest.mark.parametrize("digit", ["²", "٣", "７"])
def test_unicode_digits_rejected_cleanly(digit):
    rejected(f"SELECT COUNT(*) FROM cdr_history WHERE call_time >= SYSDATE - INTERVAL '{digit}' DAY")


GOOD_FORMS = [
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= CAST('2026-11-10' AS DATE)",
    "SELECT COUNT(*) FROM cdr_history WHERE call_time >= TRUNC(SYSDATE,'DD') - 7",
]


@pytest.mark.parametrize("sql", GOOD_FORMS)
def test_cast_and_trunc_dd_bounds(sql):
    E(sql)


def test_cast_bound_range_checked():
    rejected("SELECT COUNT(*) FROM cdr_history WHERE call_time >= CAST('2026-01-01' AS DATE)")
    rejected("SELECT COUNT(*) FROM cdr_history WHERE call_time >= TRUNC(SYSDATE,'MM') - 7")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT a FROM t ORDER BY a FETCH FIRST 10 ROWS WITH TIES",
        "SELECT a FROM t FETCH FIRST 10 PERCENT ROWS ONLY",
        "SELECT a FROM t FETCH FIRST ROWS ONLY",
    ],
)
def test_limit_unusual_fetch_rejected(sql):
    with pytest.raises(QueryRejected) as info:
        apply_row_limit(P(sql), 1000)
    assert "số dòng" in info.value.hint


def test_limit_strips_optimizer_hints():
    out = apply_row_limit(P("SELECT /*+ FULL(c) */ a FROM cdr_history c"), 1000)
    assert "/*" not in out and "FULL" not in out.upper()


def test_limit_mariadb_outputs():
    assert apply_row_limit(PM("SELECT a FROM t LIMIT 10 OFFSET 3"), 1000) == "SELECT a FROM t LIMIT 10 OFFSET 3"
    assert (
        apply_row_limit(PM("SELECT a FROM t UNION ALL SELECT a FROM u LIMIT 5000"), 1000)
        == "SELECT a FROM t UNION ALL SELECT a FROM u LIMIT 1000"
    )


def test_non_ascii_digits_in_date_literal_rejected():
    rejected("SELECT COUNT(*) FROM cdr_history WHERE call_time >= '２０２６-11-10'")
    rejected("SELECT COUNT(*) FROM cdr_history WHERE call_time >= TO_DATE('２０２６1110','YYYYMMDD')")


def test_apply_row_limit_rejects_unsupported_construct():
    # Cây có PIVOT nhưng sinh theo dialect MariaDB: phải từ chối, không lặng lẽ bỏ PIVOT.
    from dataclasses import replace
    from mcp_database.guard.parser import DIALECTS
    parsed = P("SELECT * FROM t PIVOT (SUM(a) FOR b IN (1, 2))")
    bad = replace(parsed, dialect=DIALECTS["mariadb"])
    with pytest.raises(QueryRejected) as info:
        apply_row_limit(bad, 10)
    assert "không hỗ trợ" in info.value.reason
