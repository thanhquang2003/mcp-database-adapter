"""Connector MariaDB (PyMySQL)."""
from __future__ import annotations

import pymysql

from ..config import SystemProfile
from ..names import norm
from .base import (ColumnInfo, IndexInfo, PlanSummary, QueryResult, TableInfo,
                   close_quietly, fetch_limited, get_credentials, parse_table_name,
                   sanitize_errors)

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

    def _open(self, timeout_seconds: int):
        user, password = get_credentials(self.profile)
        p = self.profile
        conn = pymysql.connect(host=p.host, port=p.port, user=user, password=password,
                               database=p.database, charset="utf8mb4", autocommit=False,
                               connect_timeout=10)
        cur = conn.cursor()
        cur.execute(f"SET SESSION max_statement_time = {int(timeout_seconds)}")
        cur.execute("SET SESSION TRANSACTION READ ONLY")
        cur.execute("START TRANSACTION")
        return conn, cur

    def execute(self, sql: str, max_rows: int, timeout_seconds: int) -> QueryResult:
        conn = None
        try:
            with sanitize_errors(TIMEOUT_CODES, timeout_seconds):
                conn, cur = self._open(timeout_seconds)
                cur.execute(sql)
                return fetch_limited(cur, max_rows)
        finally:
            if conn is not None:
                close_quietly(conn)

    def explain(self, sql: str) -> PlanSummary:
        timeout = self.profile.guard.timeout_seconds
        conn = None
        try:
            with sanitize_errors(TIMEOUT_CODES, timeout):
                conn, cur = self._open(timeout)
                cur.execute(f"EXPLAIN {sql}")
                names = [d[0].lower() for d in (cur.description or [])]
                rows = [r if isinstance(r, dict) else dict(zip(names, r))
                        for r in cur.fetchall()]
                return summarize_mariadb_explain(rows)
        finally:
            if conn is not None:
                close_quietly(conn)

    def table_info(self, table: str) -> TableInfo:
        schema, name = parse_table_name(table)
        timeout = self.profile.guard.timeout_seconds
        conn = None
        try:
            with sanitize_errors(TIMEOUT_CODES, timeout):
                conn, cur = self._open(timeout)
                b = (schema, name)
                cur.execute("SELECT column_name, column_type, is_nullable "
                            "FROM information_schema.columns "
                            "WHERE table_schema = COALESCE(%s, DATABASE()) AND table_name = %s "
                            "ORDER BY ordinal_position", b)
                cols = [ColumnInfo(r[0], r[1], r[2] == "YES") for r in cur.fetchall()]
                if not cols:
                    raise LookupError(f"Không tìm thấy bảng '{table}'")
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
        finally:
            if conn is not None:
                close_quietly(conn)
