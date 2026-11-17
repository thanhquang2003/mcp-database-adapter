from mcp_database.db.mariadb import summarize_mariadb_explain
from mcp_database.db.oracle import summarize_oracle_plan


def test_oracle_full_scan():
    p = summarize_oracle_plan([("SELECT STATEMENT", "", None, 1200, 50),
                               ("TABLE ACCESS", "FULL", "CDR_HISTORY", 1200, 50)])
    assert p.cost == 1200 and p.est_rows == 50 and p.full_scans == ["CDR_HISTORY"]


def test_oracle_index_no_full_scan():
    p = summarize_oracle_plan([("SELECT STATEMENT", "", None, 5, 1),
                               ("TABLE ACCESS", "BY INDEX ROWID", "T", 5, 1),
                               ("INDEX", "RANGE SCAN", "T_IX", 2, 1)])
    assert p.full_scans == []


def test_oracle_est_rows_is_max_cardinality_cost_is_root():
    # Gốc chỉ trả 1 dòng (COUNT) nhưng bên dưới đọc 40 triệu dòng.
    p = summarize_oracle_plan([("SELECT STATEMENT", "", None, 900, 1),
                               ("SORT", "AGGREGATE", None, 900, 1),
                               ("TABLE ACCESS", "FULL", "CDR_HISTORY", 900, 40_000_000)])
    assert p.est_rows == 40_000_000 and p.cost == 900


def test_oracle_other_full_scan_operations():
    # object_name của dòng INDEX đã được explain() quy về tên bảng (qua all_indexes).
    p = summarize_oracle_plan([("SELECT STATEMENT", "", None, 9, 9),
                               ("INDEX", "FAST FULL SCAN", "CDR_HISTORY", 9, 9),
                               ("MAT_VIEW ACCESS", "FULL", "MV_DAILY", 9, 9),
                               ("INDEX", "FULL SCAN", "USERS", 9, 9),
                               ("INDEX", "FULL SCAN (MIN/MAX)", "SMALL", 1, 1)])
    assert p.full_scans == ["CDR_HISTORY", "MV_DAILY", "USERS"]


def test_mariadb_explain():
    p = summarize_mariadb_explain([{"id": 1, "table": "cdr_history", "type": "ALL", "rows": 1000000},
                                   {"id": 1, "table": "u", "type": "ref", "rows": 2}])
    assert p.est_rows == 2000000 and p.full_scans == ["CDR_HISTORY"] and p.cost is None


def test_mariadb_explain_max_across_ids():
    # UNION: hai nhánh độc lập -> lấy max, không nhân chéo.
    p = summarize_mariadb_explain([
        {"id": 1, "select_type": "PRIMARY", "table": "a", "type": "ALL", "rows": 1000},
        {"id": 1, "select_type": "PRIMARY", "table": "b", "type": "ref", "rows": 3},
        {"id": 2, "select_type": "UNION", "table": "c", "type": "ALL", "rows": 5000},
        {"id": None, "select_type": "UNION RESULT", "table": "<union1,2>", "type": "ALL", "rows": None},
    ])
    assert p.est_rows == 5000
    assert "id=2" in p.text


def test_mariadb_explain_no_rows():
    p = summarize_mariadb_explain([{"table": None, "type": None, "rows": None}])
    assert p.est_rows is None and p.full_scans == []


def test_oracle_more_full_scan_variants():
    p = summarize_oracle_plan([("SELECT STATEMENT", "", None, 9, 9),
                               ("INDEX", "FULL SCAN DESCENDING", "A", 9, 9),
                               ("BITMAP INDEX", "FULL SCAN", "B", 9, 9),
                               ("BITMAP INDEX", "FAST FULL SCAN", "C", 9, 9),
                               ("MAT_VIEW REWRITE ACCESS", "FULL", "D", 9, 9),
                               ("INDEX", "FULL SCAN DESCENDING (MIN/MAX)", "E", 1, 1),
                               ("BITMAP INDEX", "SINGLE VALUE", "F", 1, 1),
                               ("INDEX", "RANGE SCAN DESCENDING", "G", 1, 1)])
    assert p.full_scans == ["A", "B", "C", "D"]
