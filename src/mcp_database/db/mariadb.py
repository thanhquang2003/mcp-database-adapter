"""Connector MariaDB (PyMySQL).

READ ONLY ở phía session chỉ là lớp dự phòng; tài khoản DB dùng cho server nên chỉ có quyền
SELECT. Lớp này từ chối mọi câu lệnh không trả về tập kết quả.
"""
from __future__ import annotations

import pymysql

from ..config import SystemProfile
from ..names import norm
from .base import (ColumnInfo, IndexInfo, PlanSummary, QueryResult, TableInfo,
                   TableNotFound, get_credentials, parse_table_name, run_query, session)

# 1969: Query execution was interrupted (max_statement_time).
TIMEOUT_CODES = frozenset({1969})


def summarize_mariadb_explain(rows: list[dict]) -> PlanSummary:
    full: list[str] = []
    est: int | None = None
    for r in rows:
        if r.get("type") == "ALL" and r.get("table"):
            name = norm(str(r["table"]))
            if name not in full:
                full.append(name)
        n = r.get("rows")
        if n is not None:
            est = int(n) if est is None else est * int(n)
    text = "\n".join(
        f"{r.get('table')}: type={r.get('type')} rows={r.get('rows')}" for r in rows)
    return PlanSummary(cost=None, est_rows=est, full_scans=full, text=text)


class MariaDBConnector:
    def __init__(self, profile: SystemProfile):
        self.profile = profile

    def _connect(self):
        user, password = get_credentials(self.profile)
        p = self.profile
        return pymysql.connect(host=p.host, port=p.port, user=user, password=password,
                               database=p.database, charset="utf8mb4", autocommit=False,
                               connect_timeout=10)

    def _session(self, timeout_seconds: int, streaming: bool = False):
        def setup(conn):
            # SSCursor: không nạp cả tập kết quả vào bộ nhớ trước khi fetchmany.
            cur = conn.cursor(pymysql.cursors.SSCursor) if streaming else conn.cursor()
            cur.execute(f"SET SESSION max_statement_time = {int(timeout_seconds)}")
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            cur.execute("START TRANSACTION")
            return cur
        return session(self._connect, setup, TIMEOUT_CODES, timeout_seconds)

    def execute(self, sql: str, max_rows: int, timeout_seconds: int) -> QueryResult:
        with self._session(timeout_seconds, streaming=True) as s:
            return run_query(s, sql, max_rows, abandon_on_truncate=True)

    def explain(self, sql: str) -> PlanSummary:
        with self._session(self.profile.guard.timeout_seconds) as s:
            cur = s.cursor
            cur.execute(f"EXPLAIN {sql}")
            names = [d[0].lower() for d in (cur.description or [])]
            rows = [r if isinstance(r, dict) else dict(zip(names, r)) for r in cur.fetchall()]
            return summarize_mariadb_explain(rows)

    def table_info(self, table: str) -> TableInfo:
        schema, name = parse_table_name(table)
        with self._session(self.profile.guard.timeout_seconds) as s:
            cur = s.cursor
            b = (schema, name)
            cur.execute("SELECT column_name, column_type, is_nullable "
                        "FROM information_schema.columns "
                        "WHERE table_schema = COALESCE(%s, DATABASE()) AND table_name = %s "
                        "ORDER BY ordinal_position", b)
            cols = [ColumnInfo(r[0], r[1], r[2] == "YES") for r in cur.fetchall()]
            if not cols:
                raise TableNotFound(f"Không tìm thấy bảng '{table}'")
            cur.execute("SELECT index_name, non_unique, column_name "
                        "FROM information_schema.statistics "
                        "WHERE table_schema = COALESCE(%s, DATABASE()) AND table_name = %s "
                        "ORDER BY index_name, seq_in_index", b)
            idx: dict[str, IndexInfo] = {}
            for iname, non_unique, col in cur.fetchall():
                idx.setdefault(iname, IndexInfo(iname, [], int(non_unique) == 0)).columns.append(col)
            cur.execute("SELECT table_rows FROM information_schema.tables "
                        "WHERE table_schema = COALESCE(%s, DATABASE()) AND table_name = %s", b)
            row = cur.fetchone()
            est = int(row[0]) if row and row[0] is not None else None
            return TableInfo(cols, list(idx.values()), est)
