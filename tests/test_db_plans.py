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


def test_mariadb_explain():
    p = summarize_mariadb_explain([{"table": "cdr_history", "type": "ALL", "rows": 1000000},
                                   {"table": "u", "type": "ref", "rows": 2}])
    assert p.est_rows == 2000000 and p.full_scans == ["CDR_HISTORY"] and p.cost is None


def test_mariadb_explain_no_rows():
    p = summarize_mariadb_explain([{"table": None, "type": None, "rows": None}])
    assert p.est_rows is None and p.full_scans == []
