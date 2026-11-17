"""Connector Oracle (python-oracledb, thin mode)."""
from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import oracledb

from ..config import SystemProfile
from ..names import norm
from .base import (ColumnInfo, IndexInfo, PlanSummary, QueryResult, TableInfo,
                   close_quietly, fetch_limited, get_credentials, parse_table_name,
                   sanitize_errors)

# DPY-4024: call_timeout của driver; ORA-01013 / ORA-03156: bị ngắt do hết thời gian.
TIMEOUT_CODES = frozenset({"DPY-4024", "ORA-01013", "ORA-03156"})

NLS_SQL = ("ALTER SESSION SET NLS_DATE_FORMAT = 'YYYY-MM-DD HH24:MI:SS' "
           "NLS_TIMESTAMP_FORMAT = 'YYYY-MM-DD HH24:MI:SS.FF' "
           "NLS_TIMESTAMP_TZ_FORMAT = 'YYYY-MM-DD HH24:MI:SS.FF TZH:TZR'")


def summarize_oracle_plan(rows: list[tuple]) -> PlanSummary:
    """rows: (operation, options, object_name, cost, cardinality) theo thứ tự id."""
    cost = est = None
    if rows:
        cost = int(rows[0][3]) if rows[0][3] is not None else None
        est = int(rows[0][4]) if rows[0][4] is not None else None
    full: list[str] = []
    for op, options, obj, _c, _k in rows:
        if (op or "").upper().startswith("TABLE ACCESS") and "FULL" in (options or "").upper() and obj:
            name = norm(obj)
            if name not in full:
                full.append(name)
    return PlanSummary(cost=cost, est_rows=est, full_scans=full, text="")


class OracleConnector:
    def __init__(self, profile: SystemProfile):
        self.profile = profile

    def _connect(self):
        user, password = get_credentials(self.profile)
        p = self.profile
        return oracledb.connect(user=user, password=password,
                                dsn=f"{p.host}:{p.port}/{p.database}")

    def _prepare(self, conn, cur, timeout_seconds: int, read_only: bool) -> None:
        conn.call_timeout = timeout_seconds * 1000
        cur.execute(NLS_SQL)
        if read_only:
            cur.execute("SET TRANSACTION READ ONLY")

    def execute(self, sql: str, max_rows: int, timeout_seconds: int) -> QueryResult:
        conn = None
        try:
            with sanitize_errors(TIMEOUT_CODES, timeout_seconds):
                conn = self._connect()
                cur = conn.cursor()
                self._prepare(conn, cur, timeout_seconds, read_only=True)
                cur.execute(sql)
                return fetch_limited(cur, max_rows)
        finally:
            if conn is not None:
                close_quietly(conn)

    def explain(self, sql: str) -> PlanSummary:
        # EXPLAIN PLAN ghi vào PLAN_TABLE nên không đặt được READ ONLY; câu SQL không được chạy.
        # Dữ liệu plan chưa commit và bị rollback khi đóng kết nối nên không cần DELETE.
        timeout = self.profile.guard.timeout_seconds
        sid = uuid4().hex[:30]  # chỉ ký tự hex, an toàn để nhúng literal
        conn = None
        try:
            with sanitize_errors(TIMEOUT_CODES, timeout):
                conn = self._connect()
                cur = conn.cursor()
                self._prepare(conn, cur, timeout, read_only=False)
                cur.execute(f"EXPLAIN PLAN SET STATEMENT_ID = '{sid}' FOR {sql}")
                cur.execute("SELECT operation, options, object_name, cost, cardinality "
                            "FROM plan_table WHERE statement_id = :id ORDER BY id", {"id": sid})
                plan = summarize_oracle_plan(cur.fetchall())
                cur.execute("SELECT plan_table_output FROM TABLE("
                            "DBMS_XPLAN.DISPLAY('PLAN_TABLE', :id, 'BASIC'))", {"id": sid})
                text = "\n".join(str(r[0]) for r in cur.fetchall())
                return replace(plan, text=text)
        finally:
            if conn is not None:
                close_quietly(conn)

    def table_info(self, table: str) -> TableInfo:
        schema, name = parse_table_name(table)
        timeout = self.profile.guard.timeout_seconds
        conn = None
        try:
            with sanitize_errors(TIMEOUT_CODES, timeout):
                conn = self._connect()
                cur = conn.cursor()
                self._prepare(conn, cur, timeout, read_only=True)
                if schema:
                    owner = schema.upper()
                else:
                    cur.execute("SELECT SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') FROM dual")
                    owner = cur.fetchone()[0]
                b = {"owner": owner, "t": name.upper()}
                cur.execute("SELECT column_name, data_type, nullable FROM all_tab_columns "
                            "WHERE owner = :owner AND table_name = :t ORDER BY column_id", b)
                cols = [ColumnInfo(r[0], r[1], r[2] == "Y") for r in cur.fetchall()]
                if not cols:
                    raise LookupError(f"Không tìm thấy bảng '{table}'")
                cur.execute("SELECT i.index_name, i.uniqueness, c.column_name "
                            "FROM all_indexes i JOIN all_ind_columns c "
                            "ON c.index_owner = i.owner AND c.index_name = i.index_name "
                            "WHERE i.table_owner = :owner AND i.table_name = :t "
                            "ORDER BY i.index_name, c.column_position", b)
                idx: dict[str, IndexInfo] = {}
                for iname, uniq, col in cur.fetchall():
                    idx.setdefault(iname, IndexInfo(iname, [], uniq == "UNIQUE")).columns.append(col)
                cur.execute("SELECT num_rows FROM all_tables "
                            "WHERE owner = :owner AND table_name = :t", b)
                row = cur.fetchone()
                est = int(row[0]) if row and row[0] is not None else None
                return TableInfo(cols, list(idx.values()), est)
        finally:
            if conn is not None:
                close_quietly(conn)
